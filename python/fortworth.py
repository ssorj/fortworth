from plano import *

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

_file_service_host = "192.168.86.27"
_file_service_url = "http://{0}:7070".format(_file_service_host)
_tag_service_url = "http://192.168.86.27:9090"

_yum_repo_template = """
[{build_repo}/{build_tag}/{build_id}]
name={build_repo}/{build_tag}/{build_id}
baseurl={file_repo_url}
enabled=1
gpgcheck=0
skip_if_unavailable=1

# Yum repo install command:
# curl {file_repo_url}/config.txt -o /etc/yum.repos.d/{build_repo}.repo
#
# Build URL:
# {build_url}
"""

def stagger_get_tag(repo, tag):
    url = "{0}/api/repos/{1}/tags/{2}".format(_tag_service_url, repo, tag)
    return http_get_json(url)

def stagger_put_tag(repo, tag, data):
    url = "{0}/api/repos/{1}/tags/{2}".format(_tag_service_url, repo, tag)
    return http_put_json(url, data)

def stagger_get_artifact(repo, tag, artifact):
    url = "{0}/api/repos/{1}/tags/{2}/artifacts/{3}".format(_tag_service_url, repo, tag, artifact)
    return http_get_json(url)

def stagger_put_artifact(repo, tag, artifact, data):
    url = "{0}/api/repos/{1}/tags/{2}/artifacts/{3}".format(_tag_service_url, repo, tag, artifact)
    return http_put_json(url, data)

def rpm_make_yum_repo_config(build_repo, build_tag, build_id, build_url):
    file_repo_url = "{0}/{1}/{2}/{3}".format(_file_service_url, build_repo, build_tag, build_id)
    return _yum_repo_template.lstrip().format(**locals())

def rpm_make_tag(spec_file, build_repo, build_tag, build_id, build_url):
    file_repo_url = "{0}/{1}/{2}/{3}".format(_file_service_url, build_repo, build_tag, build_id)
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
        url = "{0}/config.txt".format(yum_repo_url)

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

# Requires /root/keys/files.key
def rpm_publish(spec_file, build_dir, build_repo, build_tag, build_id, build_url):
    rpms_dir = join(build_dir, "RPMS")
    repo_stem = join("repos", build_repo, build_tag, build_id)
    repo_dir = join(build_dir, repo_stem)

    copy(rpms_dir, repo_dir)
    call("createrepo {0}", repo_dir)

    yum_repo_config = rpm_make_yum_repo_config(build_repo, build_tag, build_id, build_url)
    yum_repo_file = join(repo_dir, "config.txt".format(build_repo))

    write(yum_repo_file, yum_repo_config)

    # Skip developer test builds
    if build_id != "0":
        call("scp -o StrictHostKeyChecking=no -i /root/keys/files.key -r {0} files@{1}:{2}",
             repo_dir, _file_service_host, repo_stem)

    tag_data = rpm_make_tag(spec_file, build_repo, build_tag, build_id, build_url)

    # Skip developer test builds
    if build_id != "0":
        stagger_put_tag(build_repo, build_tag, tag_data)

def maven_make_tag(source_dir, build_dir, build_repo, build_tag, build_id, build_url):
    build_dir = absolute_path(build_dir)
    repo_dir = join(build_dir, "maven-repository")

    with working_dir(source_dir):
        records = call_for_stdout("mvn -q -Dmaven.repo.local={0} -Dexec.executable=echo -Dexec.args='${{project.groupId}},${{project.artifactId}},${{project.version}}' exec:exec", repo_dir)

    file_repo_url = "{0}/{1}/{2}/{3}/maven-repository".format(_file_service_url, build_repo, build_tag, build_id)
    artifacts = dict()

    for record in records.split():
        group_id, artifact_id, version = record.split(",")

        artifact = {
            "type": "maven",
            "repository_url": file_repo_url,
            "group_id": group_id,
            "artifact_id": artifact_id,
            "version": version,
        }

        artifacts[artifact_id] = artifact

    # commit_id, commit_url
    tag = {
        "build_id": build_id,
        "build_url": build_url,
        "artifacts": artifacts,
    }

    return tag

# mvn versions:use-dep-version -Dincludes=junit:junit -DdepVersion=1.0 -DforceVersion=true

def maven_build(source_dir, build_dir, build_id, repo_urls=[], properties={}):
    repo_dir = absolute_path(join(build_dir, "maven-repository"))
    settings_file = _make_settings_file(repo_urls)

    with working_dir(source_dir):
        commit = git_current_commit(".")
        version = call_for_stdout("mvn -q -Dexec.executable=echo -Dexec.args='${{project.version}}' --non-recursive exec:exec")
        version = version.strip()
        version = version.replace("SNAPSHOT", "{0}.{1}".format(build_id, commit[:8]))

        call("mvn versions:set -DnewVersion={0}", version)

        options = [
            "-U",
            "-DskipTests",
            "-Dmaven.repo.local={0}".format(repo_dir),
            "-gs", settings_file,
        ]

        for name, value in properties.items():
            options.append("-D{0}={1}".format(name, value))

        call("mvn {0} install", " ".join(options))

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

# Requires /root/keys/files.key
def maven_publish(source_dir, build_dir, build_repo, build_tag, build_id, build_url):
    repo_stem = join("repos", build_repo, build_tag, build_id)

    if build_id != "0":
        call("scp -o StrictHostKeyChecking=no -i /root/keys/files.key -r {0} files@{1}:{2}",
             build_dir, _file_service_host, repo_stem)

    tag_data = maven_make_tag(source_dir, build_dir, build_repo, build_tag, build_id, build_url)

    # Skip developer test builds
    if build_id != "0":
        stagger_put_tag(build_repo, build_tag, tag_data)
