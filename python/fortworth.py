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

def http_get(url, output_file):
    call("curl -f -H 'Expect:' {0} -o {1}", url, output_file)

# XXX http_get(url, output_file=None)
# XXX http_put(url, input)
# XXX Use json codec directly

def http_get_json(url):
    with temp_file() as f:
        http_get(url, f)
        return read_json(f)

def http_put_json(url, data):
    with temp_file() as f:
        write_json(f, data)
        call("curl -f -X PUT -H 'Expect:' {0} -d @{1}", url, f)

_file_service_url = "http://192.168.86.27:7070"
_tag_service_url = "http://192.168.86.27:9090"

_yum_repo_template = """
[{build_repo}-{build_id}]
name={build_repo}-{build_id}
baseurl={file_repo_url}
enabled=1
gpgcheck=0
skip_if_unavailable=1

# Repo install command:
# curl {file_repo_url}/{build_repo}.repo -o /etc/yum.repos.d/{build_repo}.repo
#
# Build URL:
# {build_url}
"""

def stagger_get_tag(build_repo, build_tag):
    url = "{0}/api/repos/{1}/tags/{2}".format(_tag_service_url, build_repo, build_tag)
    return http_get_json(url)

def stagger_put_tag(build_repo, build_tag, tag_data):
    url = "{0}/api/repos/{1}/tags/{2}".format(_tag_service_url, build_repo, build_tag)
    http_put_json(url, tag_data)

def rpm_make_yum_repo_config(build_repo, build_id, build_url):
    file_repo_url = "{0}/{1}/{2}".format(_file_service_url, build_repo, build_id)
    return _yum_repo_template.lstrip().format(**locals())

def rpm_make_tag(spec_file, build_repo, build_id, build_url):
    file_repo_url = "{0}/{1}/{2}".format(_file_service_url, build_repo, build_id)
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

def rpm_install_tag_packages(build_repo, build_tag, *packages):
    tag_data = stagger_get_tag(build_repo, build_tag)

    for package in packages:
        yum_repo_url = tag_data["artifacts"][package]["repository_url"]
        url = "{0}/{1}.repo".format(yum_repo_url, build_repo)

        http_get(url, "/etc/yum.repos.d/{0}.repo".format(build_repo))

        call("yum -y install {0}", package)

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

def rpm_publish(spec_file, build_dir, build_repo, build_tag, build_id, build_url):
    rpms_dir = join(build_dir, "RPMS")
    output_dir = join("/output", build_repo, build_id)

    copy(rpms_dir, output_dir)
    call("createrepo {0}", output_dir)

    yum_repo_config = rpm_make_yum_repo_config(build_repo, build_id, build_url)
    yum_repo_file = join(output_dir, "{0}.repo".format(build_repo))

    write(yum_repo_file, yum_repo_config)

    tag_data = rpm_make_tag(spec_file, build_repo, build_id, build_url)
    url = "{0}/api/repos/{1}/tags/{2}".format(_tag_service_url, build_repo, build_tag)

    # Skip developer test builds
    if build_id != "0":
        stagger_put_tag(build_repo, build_tag, tag_data)
