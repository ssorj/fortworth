from plano import *

def distro_key():
    fields = read("/etc/system-release-cpe")
    fields = fields.strip().split(":")

    project, os, version = fields[2:5]

    if project == "centos":
        os = "el"

    return "{0}{1}".format(os, version)

def git_current_commit(checkout_dir):
    with working_dir(checkout_dir):
        return call_for_stdout("git rev-parse HEAD").strip()

def git_make_archive(checkout_dir, output_dir, archive_stem):
    output_dir = absolute_path(output_dir)
    output_file = join(output_dir, "{0}.tar.gz".format(archive_stem))

    make_dir(output_dir)

    with working_dir(checkout_dir):
        call("git archive --output {0} --prefix {1}/ HEAD", output_file, archive_stem)

    return output_file

def http_put_json(url, data):
    with temp_file() as f:
        write_json(f, data)
        call("curl -f -X PUT -H 'Expect:' {0} -d @{1}", url, f)

_yum_repo_template = """
[{build_repo}-snapshot]
name={build_repo}-snapshot-{build_id}
baseurl={file_repo_url}
enabled=1
gpgcheck=0
skip_if_unavailable=1

# curl {file_repo_url}/{build_repo}-snapshot.repo -o /etc/yum.repos.d/{build_repo}-snapshot.repo
# {build_url}
"""

def rpm_make_yum_repo_config(output_dir, build_repo, build_id, build_url, file_repo_url):
    content = _yum_repo_template.lstrip().format(**locals())
    output_file = join(output_dir, "{0}-snapshot.repo".format(build_repo))

    write(output_file, content)

def rpm_make_tag(spec_file, build_repo, build_id, build_url, file_repo_url):
    records = call_for_stdout("rpm -q --qf '%{{name}},%{{version}},%{{release}}\n' --specfile {0}",
                              spec_file)
    artifacts = dict()

    for record in records.split():
        name, version, release = record.split(",")

        artifact = {
            "type": "rpm",
            "repository_url": file_repo_url,
            "name": name,
            "version": version,
            "release": release,
        }

        artifacts[name] = artifact

    # commit_id, commit_url
    tag = {
        "build_id": build_id,
        "build_url": build_url,
        "artifacts": artifacts,
    }

    return tag

def rpm_configure(input_spec_file, output_spec_file, source_dir, build_id):
    assert input_spec_file.endswith(".in"), input_spec_file
    assert is_dir(join(source_dir, ".git"))

    commit = git_current_commit(source_dir)
    release = "0.{0}.{1}".format(build_id, commit[:8])

    configure_file(input_spec_file, output_spec_file, release=release)

def rpm_build(spec_file, source_dir, build_dir, build_id):
    records = call_for_stdout("rpm -q --qf '%{{name}}-%{{version}}\n' --specfile {0}", spec_file)
    archive_stem = records.split()[0]

    git_make_archive(source_dir, join(build_dir, "SOURCES"), archive_stem)

    call("rpmbuild -D '_topdir {0}' -ba {1}", absolute_path(build_dir), spec_file)

_file_service_url = "file:/var/tmp/output"
_tag_service_url = "http://192.168.86.27:9090"

def rpm_publish(spec_file, build_dir, build_repo, build_tag, build_id, build_url):
    output_dir_stem = join(build_repo, build_id)
    output_dir = join("/output", output_dir_stem)
    file_repo_url = "{0}/{1}".format(_file_service_url, output_dir_stem)

    rpms_dir = join(build_dir, "RPMS")

    rpm_make_yum_repo_config(rpms_dir, build_repo, build_id, build_url, file_repo_url)
    call("createrepo {0}", rpms_dir)
    copy(rpms_dir, output_dir)

    tag = rpm_make_tag(spec_file, build_repo, build_id, build_url, file_repo_url)
    url = "{0}/api/repos/{1}/tags/{2}".format(_tag_service_url, build_repo, build_tag)

    # Skip developer test builds
    if build_id == "0":
        return

    http_put_json(url, tag)
