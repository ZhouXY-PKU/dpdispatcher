import os
import shutil
import time
import uuid
import warnings

from dpdispatcher import dlog
from dpdispatcher.dpcloudserver import Client, zip_file
from dpdispatcher.dpcloudserver.config import ALI_OSS_BUCKET_URL
from dpdispatcher.JobStatus import JobStatus
from dpdispatcher.machine import Machine

shell_script_header_template = """
#!/bin/bash -l
"""


class Bohrium(Machine):
    alias = ("Lebesgue", "DpCloudServer")

    def __init__(self, context):
        self.context = context
        self.input_data = context.remote_profile["input_data"].copy()
        self.api_version = 2
        if "api_version" in self.input_data:
            self.api_version = self.input_data.get("api_version", 2)
        if "lebesgue_version" in self.input_data:
            self.api_version = self.input_data.get("lebesgue_version", 2)
        self.grouped = self.input_data.get("grouped", False)
        email = context.remote_profile.get("email", None)
        phone = context.remote_profile.get("phone", None)
        username = context.remote_profile.get("username", None)
        password = context.remote_profile.get("password", None)
        self.retry_count = context.remote_profile.get("retry_count", 3)
        self.ignore_exit_code = context.remote_profile.get("ignore_exit_code", True)

        ticket = os.environ.get("BOHR_TICKET", None)
        if ticket:
            self.api = Client(ticket=ticket)
            self.group_id = None
            return

        if email is None and username is not None:
            raise DeprecationWarning(
                "username is no longer support in current version, "
                "please consider use email instead of username."
            )
        if email is None and phone is None:
            raise ValueError(
                "can not find email/phone number in remote_profile, please check your machine file."
            )

        if password is None:
            raise ValueError(
                "can not find password in remote_profile, please check your machine file."
            )
        if self.api_version == 1:
            raise DeprecationWarning(
                "api version 1 is deprecated. Use version 2 instead."
            )

        account = email
        if email is None:
            account = phone
        self.api = Client(account, password)

        self.group_id = None

    def gen_script(self, job):
        shell_script = super(DpCloudServer, self).gen_script(job)
        return shell_script

    def gen_script_header(self, job):
        shell_script_header = shell_script_header_template
        return shell_script_header

    def gen_local_script(self, job):
        script_str = self.gen_script(job)
        script_file_name = job.script_file_name
        self.context.write_local_file(fname=script_file_name, write_str=script_str)
        return script_file_name

    def _gen_backward_files_list(self, job):
        result_file_list = []
        # result_file_list.extend(job.backward_common_files)
        for task in job.job_task_list:
            result_file_list.extend(
                [os.path.join(task.task_work_path, b_f) for b_f in task.backward_files]
            )
        result_file_list = list(set(result_file_list))
        return result_file_list

    def _gen_oss_path(self, job, zip_filename):
        if hasattr(job, "upload_path") and job.upload_path:
            return job.upload_path
        else:
            program_id = self.context.remote_profile.get("program_id")
            program_id = self.context.remote_profile.get("project_id", program_id)
            if program_id is None:
                dlog.info(
                    "can not find program id in remote profile, upload to default program id."
                )
                program_id = 0
            uid = uuid.uuid4()
            path = os.path.join("program", str(program_id), str(uid), zip_filename)
            setattr(job, "upload_path", path)
            return path

    def do_submit(self, job):
        self.gen_local_script(job)
        zip_filename = job.job_hash + ".zip"
        # oss_task_zip = 'indicate/' + job.job_hash + '/' + zip_filename
        oss_task_zip = self._gen_oss_path(job, zip_filename)
        job_resources = ALI_OSS_BUCKET_URL + oss_task_zip
        input_data = self.input_data.copy()

        if not input_data.get("job_resources"):
            input_data["job_resources"] = []
        input_data["job_resources"].append(job_resources)
        input_data["command"] = f"bash {job.script_file_name}"
        if not input_data.get("backward_files"):
            input_data["backward_files"] = self._gen_backward_files_list(job)
        input_data["logFiles"] = os.path.join(
            job.job_task_list[0].task_work_path, job.job_task_list[0].outlog
        )
        program_id = self.context.remote_profile.get("program_id")
        program_id = self.context.remote_profile.get("project_id", program_id)
        if program_id is None:
            warnings.warn("program_id is compulsory.")
        job_id, group_id = self.api.job_create(
            job_type=input_data["job_type"],
            oss_path=input_data["job_resources"],
            input_data=input_data,
            program_id=program_id,
            group_id=self.group_id,
        )
        if self.grouped:
            self.group_id = group_id
        job.job_id = str(job_id) + ":job_group_id:" + str(group_id)
        job_id = job.job_id
        job.job_state = JobStatus.waiting
        return job_id

    def _get_job_detail(self, job_id, group_id):
        check_return = self.api.get_job_detail(job_id)
        assert check_return is not None, (
            f"Failed to retrieve tasks information. To resubmit this job, please "
            f"try again, if this problem still exists please delete the submission "
            f"file and try again.\nYou can check submission.submission_hash in the "
            f'previous log or type `grep -rl "{job_id}:job_group_id:{group_id}" '
            f"~/.dpdispatcher/dp_cloud_server/` to find corresponding file. "
            f"You can try with command:\n    "
            f'rm $(grep -rl "{job_id}:job_group_id:{group_id}" ~/.dpdispatcher/dp_cloud_server/)'
        )
        return check_return

    def check_status(self, job):
        if job.job_id == "":
            return JobStatus.unsubmitted
        job_id = job.job_id
        group_id = None
        if isinstance(job.job_id, str) and ":job_group_id:" in job.job_id:
            group_id = None
            ids = job.job_id.split(":job_group_id:")
            job_id, group_id = int(ids[0]), int(ids[1])
            if (
                self.input_data.get("grouped")
                and ":job_group_id:" not in self.input_data
            ):
                self.group_id = group_id
            self.api_version = 2
        dlog.debug(
            f"debug: check_status; job.job_id:{job_id}; job.job_hash:{job.job_hash}"
        )
        check_return = self._get_job_detail(job_id, group_id)
        try:
            dp_job_status = check_return["status"]
        except IndexError as e:
            dlog.error(
                f"cannot find job information in bohrium for job {job.job_id}. check_return:{check_return}; retry one more time after 60 seconds"
            )
            time.sleep(60)
            retry_return = self._get_job_detail(job_id, group_id)
            try:
                dp_job_status = retry_return["status"]
            except IndexError as e:
                raise RuntimeError(
                    f"cannot find job information in bohrium for job {job.job_id} {check_return} {retry_return}"
                )

        job_state = self.map_dp_job_state(
            dp_job_status, check_return.get("exitCode", 0), self.ignore_exit_code
        )
        if job_state == JobStatus.finished:
            job_log = self.api.get_log(job_id)
            if self.input_data.get("output_log"):
                print(job_log, end="")
            self._download_job(job)
        elif self.input_data.get("output_log") and job_state == JobStatus.running:
            job_log = self.api.get_log(job_id)
            print(job_log, end="")
        return job_state

    def _download_job(self, job):
        job_url = self.api.get_job_result_url(job.job_id)
        if not job_url:
            return
        job_hash = job.job_hash
        result_filename = job_hash + "_back.zip"
        target_result_zip = os.path.join(self.context.local_root, result_filename)
        self.api.download_from_url(job_url, target_result_zip)
        zip_file.unzip_file(target_result_zip, out_dir=self.context.local_root)
        try:
            os.makedirs(os.path.join(self.context.local_root, "backup"), exist_ok=True)
            shutil.move(
                target_result_zip,
                os.path.join(
                    self.context.local_root,
                    "backup",
                    os.path.split(target_result_zip)[1],
                ),
            )
        except (OSError, shutil.Error) as e:
            dlog.exception("unable to backup file, " + str(e))

    def check_finish_tag(self, job):
        job_tag_finished = job.job_hash + "_job_tag_finished"
        dlog.info("check if job finished: ", job.job_id, job_tag_finished)
        return self.context.check_file_exists(job_tag_finished)
        # return
        # pass

    def check_if_recover(self, submission):
        return False
        # pass

    @staticmethod
    def map_dp_job_state(status, exit_code, ignore_exit_code=True):
        if isinstance(status, JobStatus):
            return status
        map_dict = {
            -1: JobStatus.terminated,
            0: JobStatus.waiting,
            1: JobStatus.running,
            2: JobStatus.finished,
            3: JobStatus.waiting,
            4: JobStatus.running,
            5: JobStatus.terminated,
            6: JobStatus.running,
            9: JobStatus.waiting,
        }
        if status not in map_dict:
            dlog.error(f"unknown job status {status}")
            return JobStatus.unknown
        if status == -1 and exit_code != 0 and ignore_exit_code:
            return JobStatus.finished
        return map_dict[status]

    def kill(self, job):
        """Kill the job.

        Parameters
        ----------
        job : Job
            job
        """
        job_id = job.job_id
        self.api.kill(job_id)

    def get_exit_code(self, job) -> int:
        job_id = self._parse_job_id(job.job_id)
        if job_id <= 0:
            raise RuntimeError(f"cannot parse job id {job.job_id}")

        check_return = self._get_job_detail(job_id, self.group_id)
        return check_return.get("exitCode", -999)  # type: ignore

    def _parse_job_id(self, str_job_id: str) -> int:
        job_id = 0
        if "job_group_id" in str_job_id:
            ids = str_job_id.split(":job_group_id:")
            job_id, _ = int(ids[0]), int(ids[1])
        return job_id


DpCloudServer = Bohrium
Lebesgue = Bohrium
