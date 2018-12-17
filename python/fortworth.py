from plano import *

_file_service_host = "192.168.86.27"
_file_service_url = "http://{0}:7070".format(_file_service_host)
_tag_service_url = "https://stagger-rhm.cloud.paas.upshift.redhat.com"

_yum_repo_config_template = """
[{repo}/{branch}/{build_id}]
name={repo}/{branch}/{build_id}
baseurl={yum_repo_url}
enabled=1
gpgcheck=0
skip_if_unavailable=1

# Yum repo install command:
#
# curl {yum_repo_url}/config.txt -o /etc/yum.repos.d/{repo}.repo
"""

def git_current_commit(checkout_dir):
    with working_dir(checkout_dir):
        return call_for_stdout("git rev-parse HEAD").strip()

def git_current_branch(checkout_dir):
    with working_dir(checkout_dir):
        return call_for_stdout("git rev-parse --abbrev-ref HEAD").strip()

def git_make_archive(checkout_dir, output_dir, archive_stem):
    output_dir = absolute_path(output_dir)
    output_file = join(output_dir, "{0}.tar.gz".format(archive_stem))

    make_dir(output_dir)

    with working_dir(checkout_dir):
        call("git archive --output {0} --prefix {1}/ HEAD", output_file, archive_stem)

    return output_file

def stagger_get_tag(repo, branch, tag):
    url = "{0}/api/repos/{1}/branches/{2}/tags/{3}".format(_tag_service_url, repo, branch, tag)
    return http_get_json(url)

def stagger_put_tag(repo, branch, tag, tag_data):
    url = "{0}/api/repos/{1}/branches/{2}/tags/{3}".format(_tag_service_url, repo, branch, tag)
    return http_put_json(url, tag_data)

def stagger_get_artifact(repo, branch, tag, artifact):
    url = "{0}/api/repos/{1}/branches/{2}/tags/{3}/artifacts/{4}".format(_tag_service_url, repo, branch, tag, artifact)
    return http_get_json(url)

def stagger_put_artifact(repo, branch, tag, artifact, artifact_data):
    url = "{0}/api/repos/{1}/branches/{2}/tags/{3}/artifacts/{4}".format(_tag_service_url, repo, branch, tag, artifact)
    return http_put_json(url, artifact_data)

# Requires /root/keys/files.key be present inside the container
def store_build(build_dir, repo, branch, build_id):
    options = "-o StrictHostKeyChecking=no -i /root/keys/files.key"
    remote_dir = join("repos", repo, branch, build_id)

    call("ssh {0} files@{1} 'rm -rf {2}; mkdir -p {3}'",
         options, _file_service_host, remote_dir, parent_dir(remote_dir))
    call("scp {0} -r {1} files@{2}:{3}", options, build_dir, _file_service_host, remote_dir)

def rpm_make_yum_repo_config(repo, branch, build_id):
    yum_repo_url = _yum_repo_url(repo, branch, build_id)
    return _yum_repo_config_template.lstrip().format(**locals())

def rpm_make_tag_data(spec_file, source_dir, repo, branch, build_id, build_url=None):
    records = call_for_stdout("rpm -q --qf '%{{name}},%{{version}},%{{release}}\n' --specfile {0}", spec_file)
    artifacts = dict()

    for record in records.split():
        name, version, release = record.split(",")

        artifact = {
            "type": "rpm",
            "repository_url": _yum_repo_url(repo, branch, build_id),
            "name": name,
            "version": version,
            "release": release,
        }

        artifacts[name] = artifact

    tag = {
        "build_id": build_id,
        "build_url": build_url,
        "commit_id": git_current_commit(source_dir),
        "files_url": _files_url(repo, branch, build_id),
        "artifacts": artifacts,
    }

    return tag

def rpm_install_tag_packages(repo, branch, tag, *packages):
    tag_data = stagger_get_tag(repo, branch, tag)

    for package in packages:
        yum_repo_url = tag_data["artifacts"][package]["repository_url"]
        url = "{0}/config.txt".format(yum_repo_url)

        http_get(url, output_file="/etc/yum.repos.d/{0}.repo".format(repo))
        call("yum -y install {0}", package)

def rpm_configure(input_spec_file, output_spec_file, source_dir, build_id):
    assert input_spec_file.endswith(".in"), input_spec_file
    assert is_dir(join(source_dir, ".git"))

    commit = git_current_commit(source_dir)
    release = "0.{0}.{1}".format(build_id, commit[:8])

    configure_file(input_spec_file, output_spec_file, release=release)

def rpm_build(spec_file, source_dir, build_dir, repo, branch, tag, build_id, build_url=None):
    records = call_for_stdout("rpm -q --qf '%{{name}}-%{{version}}\n' --specfile {0}", spec_file)
    archive_stem = records.split()[0]
    rpms_dir = join(build_dir, "RPMS")
    output_dir = make_temp_dir()
    yum_repo_dir = join(output_dir, "repo")
    yum_repo_config = rpm_make_yum_repo_config(repo, branch, build_id)
    yum_repo_file = join(yum_repo_dir, "config.txt")

    git_make_archive(source_dir, join(build_dir, "SOURCES"), archive_stem)
    call("rpmbuild -D '_topdir {0}' -ba {1}", absolute_path(build_dir), spec_file)
    copy(rpms_dir, yum_repo_dir)
    call("createrepo {0}", rpms_dir)
    write(yum_repo_file, yum_repo_config)

    tag_data = rpm_make_tag_data(spec_file, source_dir, repo, branch, build_id, build_url=build_url)

    if build_id is not None:
        store_build(output_dir, repo, branch, build_id)
        stagger_put_tag(repo, branch, tag, tag_data)

def rpm_publish(spec_file, source_dir, repo, branch, tag, build_id, build_url=None):
    tag_data = rpm_make_tag_data(spec_file, source_dir, repo, branch, build_id, build_url=build_url)

    # Skip developer test builds
    if build_id is not None:
        stagger_put_tag(repo, branch, tag, tag_data)

def maven_make_tag_data(source_dir, build_dir, repo, branch, build_id, build_url=None):
    maven_repo_dir = absolute_path(join(build_dir, "repo"))
    artifacts = dict()

    with working_dir(source_dir):
        records = call_for_stdout("mvn -q -Dmaven.repo.local={0} -Dexec.executable=echo -Dexec.args='${{project.groupId}},${{project.artifactId}},${{project.version}}' exec:exec", maven_repo_dir)

    for record in records.split():
        group_id, artifact_id, version = record.split(",")

        artifact = {
            "type": "maven",
            "repository_url": _maven_repo_url(repo, branch, build_id),
            "group_id": group_id,
            "artifact_id": artifact_id,
            "version": version,
        }

        artifacts[artifact_id] = artifact

    data = {
        "build_id": build_id,
        "build_url": build_url,
        "commit_id": git_current_commit(source_dir),
        "files_url": _files_url(repo, branch, build_id),
        "artifacts": artifacts,
    }

    return data

# mvn versions:use-dep-version -Dincludes=junit:junit -DdepVersion=1.0 -DforceVersion=true

def maven_build(source_dir, build_dir, repo, branch, tag, build_id, repo_urls=[], properties={}, build_url=None):
    maven_repo_dir = absolute_path(join(build_dir, "repo"))
    settings_file = _make_settings_file(repo_urls)

    with working_dir(source_dir):
        commit_id = git_current_commit(".")
        version = call_for_stdout("mvn -q -Dexec.executable=echo -Dexec.args='${{project.version}}' --non-recursive exec:exec")
        version = version.strip()
        version = version.replace("SNAPSHOT", "{0}.{1}".format(build_id, commit_id[:8]))

        call("mvn versions:set -DnewVersion={0}", version)

        options = [
            "-U",
            "-DskipTests",
            "-Dmaven.repo.local={0}".format(maven_repo_dir),
            "-gs", settings_file,
        ]

        for name, value in properties.items():
            options.append("-D{0}={1}".format(name, value))

        call("mvn {0} install", " ".join(options))

    tag_data = maven_make_tag_data(source_dir, build_dir, repo, branch, build_id, build_url=build_url)

    # Skip developer test builds
    if build_id is not None:
        store_build(build_dir, repo, branch, build_id)
        stagger_put_tag(repo, branch, tag, tag_data)

def _make_settings_file(repo_urls):
    repos = list()

    for i, url in enumerate(repo_urls):
        repos.append("<repository><id>repo-{0}</id><url>{1}</url></repository>".format(i, url))

    repos = "".join(repos)

    xml = """
    <settings>
      <profiles>
        <profile>
          <id>main</id>
          <repositories>
            {repos}
          </repositories>
        </profile>
      </profiles>
      <activeProfiles>
        <activeProfile>main</activeProfile>
      </activeProfiles>
    </settings>
    """

    xml = xml.strip().format(**locals())

    return write(make_temp_file(), xml)

def maven_publish(source_dir, build_dir, repo, branch, tag, build_id, build_url=None):
    tag_data = maven_make_tag_data(source_dir, build_dir, repo, branch, build_id, build_url=build_url)

    # Skip developer test builds
    if build_id is not None:
        stagger_put_tag(repo, branch, tag, tag_data)

def _files_url(repo, branch, build_id):
    return "{0}/{1}/{2}/{3}".format(_file_service_url, repo, branch, build_id)

def _yum_repo_url(repo, branch, build_id):
    return "{0}/repo".format(_files_url(repo, branch, build_id))

def _maven_repo_url(repo, branch, build_id):
    return "{0}/repo".format(_files_url(repo, branch, build_id))
