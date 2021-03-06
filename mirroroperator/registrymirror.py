from kubernetes import client
from kubernetes.client.rest import ApiException
import logging
import copy
import base64
import json
from http import HTTPStatus


LOGGER = logging.getLogger(__name__)


class RegistryMirror(object):
    def __init__(self, event_type, namespace, hostess_docker_registry,
                 hostess_docker_image, hostess_docker_tag,
                 docker_certificate_secret, **kwargs):
        self.event_type = event_type
        self.namespace = namespace
        self.hostess_docker_registry = hostess_docker_registry
        self.hostess_docker_image = hostess_docker_image
        self.hostess_docker_tag = hostess_docker_tag
        self.docker_certificate_secret = docker_certificate_secret
        self.kind = kwargs.get("kind")
        self.name = kwargs.get("metadata", {}).get("name")
        self.uid = kwargs.get("metadata", {}).get("uid")
        self.full_name = "registry-mirror-{}".format(self.name)
        self.daemon_set_name = self.full_name + "-utils"
        self.apiVersion = kwargs.get("apiVersion")
        self.upstreamUrl = kwargs.get("spec", {}).get("upstreamUrl")
        self.masqueradeUrl = kwargs.get("spec", {}).get("masqueradeUrl", "mirror-"+self.upstreamUrl)
        self.credentials_secret_name = kwargs.get(
            "spec", {}).get("credentialsSecret")
        self.image_pull_secrets = kwargs["image_pull_secrets"] or ""
        self.ca_certificate_bundle = kwargs["ca_certificate_bundle"]
        self.volume_claim_spec = kwargs.get(
            "spec",
            {},
        ).get(
            "volumeClaimTemplate",
            {},
        ).get(
            "spec",
            {},
        )

        self.labels = {
            "app": "docker-registry",
            "mirror": self.name,
        }

        self.metadata = client.V1ObjectMeta(
            namespace=self.namespace,
            name=self.full_name,
            labels=self.labels,
            owner_references=[
                client.V1OwnerReference(
                    api_version=self.apiVersion,
                    name=self.name,
                    kind=self.kind,
                    uid=self.uid,
                )
            ]
        )
        self.core_api = client.CoreV1Api()
        self.apps_api = client.AppsV1beta1Api()
        self.ext_api = client.ExtensionsV1beta1Api()

    def apply(self):
        if self.event_type != "DELETED":
            service = self.run_action_and_parse_error(
                self.core_api.read_namespaced_service,
                self.full_name,
                self.namespace
            )

            service_headless = self.run_action_and_parse_error(
                self.core_api.read_namespaced_service,
                self.full_name + "-headless",
                self.namespace
            )

            stateful_set = self.run_action_and_parse_error(
                self.apps_api.read_namespaced_stateful_set,
                self.full_name,
                self.namespace
            )

            daemon_set = self.run_action_and_parse_error(
                self.ext_api.read_namespaced_daemon_set,
                self.daemon_set_name,
                self.namespace
            )

            self.update_services(service, service_headless)
            self.update_stateful_set(stateful_set)
            self.update_daemon_set(daemon_set)

    def run_action_and_parse_error(self, func, *args, **kwargs):
        """ Helper method to avoid try/excepts all over the place + does
        the exception handling and parsing.
        (Kubernetes python client tends to just dump the details in .body)
        Args:
            func: A function which can raise ApiException.
            *args: tuple of arguments to pass to that function
            **kwargs: keyword arguments to pass to the function

        Returns: Object, return value of func

        """
        result = None
        try:
            result = func(*args, **kwargs)
        except ApiException as api_exception:
            try:
                json_error = json.loads(api_exception.body)
                code = HTTPStatus(int(json_error['code']))
                LOGGER.exception(
                    "API returned status: %s, msg: %s, method: %s",
                    code, json_error['message'], func)

            except json.decoder.JSONDecodeError as e:
                LOGGER.error("Decoder exception loading error msg: %s;"
                             "%s", api_exception.body, str(e))
        return result

    def generate_daemon_set(self, daemon_set):
        ds_pod_labels = copy.deepcopy(self.labels)
        ds_pod_labels["component"] = "hostess-certificate"
        daemon_set.metadata = copy.deepcopy(self.metadata)
        daemon_set.metadata.name = self.daemon_set_name
        daemon_set.spec = client.V1beta1DaemonSetSpec(
                min_ready_seconds=10,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels=ds_pod_labels
                    ),
                    spec=client.V1PodSpec(
                        containers=[
                            client.V1Container(
                                name="mirror-hostess",
                                env=[
                                    client.V1EnvVar(
                                        name="LOCK_FILE",
                                        value="/var/lock/hostess/mirror-hostess"),
                                    client.V1EnvVar(
                                        name="SERVICE_NAME",
                                        value=self.full_name),
                                    client.V1EnvVar(
                                        name="SERVICE_NAMESPACE",
                                        value=self.namespace),
                                    client.V1EnvVar(
                                        name="SHADOW_FQDN",
                                        value=self.masqueradeUrl),
                                    client.V1EnvVar(
                                        name="HOSTS_FILE",
                                        value="/etc/hosts_from_host"),
                                    client.V1EnvVar(
                                        name="HOSTS_FILE_BACKUP",
                                        value="/etc/hosts.backup/hosts")
                                ],
                                image="{}/{}:{}".format(
                                    self.hostess_docker_registry,
                                    self.hostess_docker_image,
                                    self.hostess_docker_tag),
                                image_pull_policy="Always",
                                resources=client.V1ResourceRequirements(
                                    requests={
                                        "memory": "32Mi", "cpu": "0.001"
                                    },
                                    limits={"memory": "128Mi", "cpu": "0.1"},
                                ),
                                volume_mounts=[
                                    client.V1VolumeMount(
                                        name="etc-hosts",
                                        mount_path="/etc/hosts_from_host"
                                    ),
                                    client.V1VolumeMount(
                                        name="etc-hosts-backup",
                                        mount_path="/etc/hosts.backup",
                                    ),
                                    client.V1VolumeMount(
                                        name="lock",
                                        mount_path="/var/lock/hostess",
                                    ),
                                ],
                            ),
                            client.V1Container(
                                name="certificate-installation",
                                args=[
                                    "cp /source/tls.crt /target/tls.crt; while :; do sleep 2073600; done"
                                ],
                                command=[
                                    "/bin/sh",
                                    "-c",
                                    "-e",
                                    "-u",
                                    "-x"
                                ],
                                image="alpine:3.5",
                                image_pull_policy="Always",
                                resources=client.V1ResourceRequirements(
                                    requests={"memory": "1Mi", "cpu": "0.001"},
                                    limits={"memory": "32Mi", "cpu": "0.1"}
                                ),
                                volume_mounts=[
                                    client.V1VolumeMount(name="docker-certs",
                                                         mount_path="/target"),
                                    client.V1VolumeMount(name="tls",
                                                         mount_path="/source",
                                                         read_only=True),
                                ],
                            )
                        ],
                        image_pull_secrets=[{"name": name} for name in
                                            self.image_pull_secrets.split(",")],
                        service_account_name="mirror-hostess",
                        termination_grace_period_seconds=2,
                        volumes=[client.V1Volume(
                            name="etc-hosts",
                            host_path=client.V1HostPathVolumeSource(
                                path="/etc/hosts"
                            )
                        ),
                            client.V1Volume(
                                name="etc-hosts-backup",
                                host_path=client.V1HostPathVolumeSource(
                                    path="/etc/hosts.backup"
                                )
                            ),
                            client.V1Volume(
                                name="lock",
                                host_path=client.V1HostPathVolumeSource(
                                    path="/var/lock/mirror-hostess"
                                ),
                            ),
                            client.V1Volume(
                                name="docker-certs",
                                host_path=client.V1HostPathVolumeSource(
                                    path="/etc/docker/certs.d/{}".format(self.masqueradeUrl)
                                ),
                            ),
                            client.V1Volume(
                                name="tls",
                                secret=client.V1SecretVolumeSource(
                                    secret_name=self.docker_certificate_secret
                                )
                            )
                        ]
                    )
                ),
                update_strategy=client.V1beta1DaemonSetUpdateStrategy(
                    type="RollingUpdate"
                )
            )
        return daemon_set

    def generate_service(self, service):
        service.spec.type = "ClusterIP"
        return service

    def generate_headless_service(self, service_headless):
        service_headless.spec.cluster_ip = 'None'
        service_headless.spec.type = "ClusterIP"
        return service_headless

    def handle_proxy_credentials(self, env):
        credentials_secret = None
        if self.credentials_secret_name:
            credentials_secret = self.run_action_and_parse_error(self.core_api.read_namespaced_secret,
                                                             self.credentials_secret_name,
                                                             self.namespace)
        if not credentials_secret:
            LOGGER.error("No secret named %s was found in the %s namespace, will use unauth access",
                         self.credentials_secret_name, self.namespace)
            return env

        encoded_user = credentials_secret.data.get("username")
        encoded_pass = credentials_secret.data.get("password")

        if not (encoded_user and encoded_pass):
            LOGGER.error("Secret %s does not contain username/password",
                         self.credentials_secret_name)
            return env
        env.append(client.V1EnvVar(name="REGISTRY_PROXY_USERNAME",
                                   value=None,
                                   value_from=client.V1EnvVarSource(
                                        secret_key_ref=client.V1SecretKeySelector(
                                            key="username",
                                            name=self.credentials_secret_name
                                        )
                                   ))
                  )
        env.append(client.V1EnvVar(name="REGISTRY_PROXY_PASSWORD",
                                   value=None,
                                   value_from=client.V1EnvVarSource(
                                        secret_key_ref=client.V1SecretKeySelector(
                                            key="password",
                                            name=self.credentials_secret_name
                                        )
                                   ))
                  )
        LOGGER.info("Secret selected + env vars set successfully")
        return env


    def generate_stateful_set(self):

        volume_claim_spec = client.V1PersistentVolumeClaimSpec(**self.volume_claim_spec)
        if not volume_claim_spec.access_modes:
            volume_claim_spec.access_modes = ["ReadWriteOnce"]
        if not volume_claim_spec.resources:
            volume_claim_spec.resources = client.V1ResourceRequirements(
                requests={"storage": "20Gi"}
            )

        stateful_set = client.V1beta1StatefulSet(
            metadata=self.metadata,
            spec=client.V1beta1StatefulSetSpec(
                # we can't update service name or pod management policy
                service_name=self.full_name + "-headless",
                pod_management_policy="Parallel",
                # we can't update volume claim templates
                volume_claim_templates=[client.V1PersistentVolumeClaim(
                    metadata=client.V1ObjectMeta(
                        name="image-store",
                    ),
                    spec=volume_claim_spec,
                )]
            )
        )

        stateful_set.spec.replicas = 2
        pod_labels = {'component': 'registry'}
        pod_labels.update(self.labels)
        volumes = []
        if self.ca_certificate_bundle:
            volumes = [
                client.V1Volume(
                    name=self.ca_certificate_bundle,
                    config_map=client.V1ConfigMapVolumeSource(
                        name=self.ca_certificate_bundle
                    )
                )
            ]
        volumes.append(
            client.V1Volume(
                name="tls",
                secret=client.V1SecretVolumeSource(
                    secret_name=self.docker_certificate_secret
                ),
            )
        )

        volumes_to_mount = [
            client.V1VolumeMount(
                name="image-store",
                mount_path="/var/lib/registry"
            ),
            client.V1VolumeMount(
                name="tls",
                mount_path="/etc/registry-certs",
                read_only=True
            )
        ]
        if self.ca_certificate_bundle:
            volumes_to_mount.append(
                client.V1VolumeMount(
                    name=self.ca_certificate_bundle,
                    mount_path="/etc/ssl/certs",
                    read_only=True
                )
            )

        env = [client.V1EnvVar(name="REGISTRY_PROXY_REMOTEURL",
                               value="https://" + self.upstreamUrl),
               client.V1EnvVar(name="REGISTRY_HTTP_ADDR",
                               value=":5000"),
               client.V1EnvVar(name="REGISTRY_HTTP_DEBUG_ADDR",
                               value="localhost:6000"),
               client.V1EnvVar(name="REGISTRY_HTTP_TLS_CERTIFICATE",
                               value="/etc/registry-certs/tls.crt"),
               client.V1EnvVar(name="REGISTRY_HTTP_TLS_KEY",
                               value="/etc/registry-certs/tls.key"),
               client.V1EnvVar(name="REGISTRY_LOG_ACCESSLOG_DISABLED",
                               value="true"),
               client.V1EnvVar(name="REGISTRY_LOG_FORMATTER",
                               value="logstash"),
               client.V1EnvVar(name="REGISTRY_STORAGE_DELETE_ENABLED",
                               value="true"),
               client.V1EnvVar(name="REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY",
                               value="/var/lib/registry")
               ]
        env = self.handle_proxy_credentials(env)

        stateful_set.spec.template = client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels=pod_labels
                    ),
                    spec=client.V1PodSpec(
                        init_containers=[
                            client.V1Container(
                                name="validate-state-file",
                                image="python:3.6-alpine",
                                env=[
                                    client.V1EnvVar(
                                        name="STATE_FILE",
                                        value="/var/lib/registry/scheduler-state.json"
                                    ),
                                    client.V1EnvVar(
                                        name="LOWER_LIMIT",
                                        value="1024"
                                    ),
                                ],
                                volume_mounts=[
                                    client.V1VolumeMount(
                                        name="image-store",
                                        mount_path="/var/lib/registry"
                                    )
                                ],
                                command=[
                                    "sh",
                                    "-e",
                                    "-c",
                                    "touch $STATE_FILE; if [[ $(stat -c \"%s\" $STATE_FILE) -lt $LOWER_LIMIT ]]; then rm -f $STATE_FILE; else cat $STATE_FILE | python -m json.tool > /dev/null 2>&1 || rm -f $STATE_FILE; fi"  # noqa
                                ]
                            )
                        ],
                        containers=[
                            client.V1Container(
                                name="registry",
                                image="registry:2.6.0",
                                env=env,
                                readiness_probe=client.V1Probe(
                                    http_get=client.V1HTTPGetAction(
                                        path="/",
                                        port=5000,
                                        scheme="HTTPS"
                                    ),
                                    initial_delay_seconds=3,
                                    period_seconds=3
                                ),
                                ports=[client.V1ContainerPort(
                                    container_port=5000,
                                    name="https"
                                )],
                                resources=client.V1ResourceRequirements(
                                    requests={"cpu": "0.1",
                                              "memory": "500Mi"},
                                    limits={"cpu": "0.5",
                                            "memory": "500Mi"}
                                ),
                                volume_mounts=volumes_to_mount,
                            )
                        ],
                        termination_grace_period_seconds=10,
                        volumes=volumes,
                    )
                )
        stateful_set.spec.update_strategy = client.V1beta1StatefulSetUpdateStrategy(type="RollingUpdate",)
        return stateful_set

    def update_services(self, service, service_headless):
        empty_service = client.V1Service(
            metadata=copy.deepcopy(self.metadata),
            spec=client.V1ServiceSpec(
                selector=self.labels,
                ports=[client.V1ServicePort(port=443, name="https", target_port="https")],
            )
        )
        if not service:
            service = self.generate_service(empty_service)
            self.run_action_and_parse_error(self.core_api.create_namespaced_service,
                                            self.namespace, service)
            LOGGER.info("Service created")

        else:
            service = self.generate_service(service)
            self.run_action_and_parse_error(
                self.core_api.replace_namespaced_service,
                service.metadata.name, self.namespace, service
            )
            LOGGER.info("Service replaced")

        if not service_headless:
            empty_service.metadata.name += "-headless"
            service_headless = self.generate_headless_service(empty_service)
            self.run_action_and_parse_error(self.core_api.create_namespaced_service,
                                            self.namespace, service_headless)
            LOGGER.info("Headless service created")

        else:
            service_headless = self.generate_headless_service(service_headless)
            self.run_action_and_parse_error(
                self.core_api.replace_namespaced_service, service_headless.metadata.name,
                self.namespace, service_headless
            )
            LOGGER.info("Headless service replaced")

    def update_stateful_set(self, stateful_set):
        if not stateful_set:
            stateful_set = self.generate_stateful_set()
            self.run_action_and_parse_error(
                self.apps_api.create_namespaced_stateful_set,
                self.namespace,
                stateful_set
            )
            LOGGER.info("Stateful set created")
        else:
            stateful_set = self.generate_stateful_set()
            self.run_action_and_parse_error(
                self.apps_api.replace_namespaced_stateful_set,
                stateful_set.metadata.name, self.namespace, stateful_set)
            LOGGER.info("Stateful set replaced")

    def update_daemon_set(self, daemon_set):
        empty_daemon_set = client.V1beta1DaemonSet()
        if not daemon_set:
            daemon_set = self.generate_daemon_set(empty_daemon_set)
            self.run_action_and_parse_error(self.ext_api.create_namespaced_daemon_set,
                                            self.namespace, daemon_set)
            LOGGER.info("Daemon set created")
        else:
            daemon_set = self.generate_daemon_set(daemon_set)
            self.run_action_and_parse_error(
                self.ext_api.replace_namespaced_daemon_set,
                daemon_set.metadata.name, self.namespace, daemon_set)
            LOGGER.info("Daemon set replaced")
