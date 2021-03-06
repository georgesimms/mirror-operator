from mirroroperator.registrymirror import RegistryMirror
from tests.kubernetes_test_case import KubernetesTestCase
from tests.kubernetes_mock_responses import (
    EMPTY_SERVICE, EMPTY_STATEFUL_SET, VALID_SECRET, EMPTY_DAEMON_SET,
    username, password, INVALID_SECRET
)
from urllib3_mock import Responses
import kubernetes
import json
import base64
from copy import deepcopy
responses = Responses('urllib3')


class CustomResourceTestCase(KubernetesTestCase):
    def setUp(self):
        super().setUp()
        self.env_var_dict = {
            "namespace": "default",
            "hostess_docker_registry": "docker.io",
            "hostess_docker_image": "ocadotechnology/mirror-hostess",
            "hostess_docker_tag": 2,
            "image_pull_secrets": None,
            "docker_certificate_secret": VALID_SECRET,
            "ca_certificate_bundle": None,
        }
        registry_kwargs = {"metadata": {"name": "hub"},
                           "spec": {"upstreamUrl": "hubtest"}}
        registry_kwargs.update(self.env_var_dict)
        self.mirror = RegistryMirror(event_type="CREATED", **registry_kwargs)

        registry_kwargs_with_credential_secret = deepcopy(registry_kwargs)
        registry_kwargs_with_credential_secret['spec']["credentialsSecret"] = "internal-mirror"
        registry_kwargs_with_credential_secret.update(self.env_var_dict)
        self.mirror_with_credential_secret = RegistryMirror(
            event_type="CREATED", **registry_kwargs_with_credential_secret)

    @responses.activate
    def test_update_service_neither_exist(self):
        '''Should create 2 empty services, then replace both of them'''
        responses.add(
            'POST',
            '/api/v1/namespaces/default/services',
            body=EMPTY_SERVICE
        )
        self.mirror.update_services(None, None)
        self.check_calls(
            ('POST', 'POST'),
            responses.calls,
            self.mirror.metadata
        )

    @responses.activate
    def test_update_service_one_exists(self):
        '''Should create one empty service and replace 2'''
        responses.add(
            'POST',
            '/api/v1/namespaces/default/services',
            body=EMPTY_SERVICE
        )
        responses.add(
            'PUT',
            '/api/v1/namespaces/default/services/registry-mirror-hub',
            ''
        )
        self.mirror.update_services(kubernetes.client.V1Service(
            metadata=self.mirror.metadata,
            spec=kubernetes.client.V1ServiceSpec()
        ), None)
        self.check_calls(
            ('PUT', 'POST'),
            responses.calls,
            self.mirror.metadata
        )

    @responses.activate
    def test_update_service_both_exist(self):
        '''Should replace both objects'''
        responses.add(
            'PUT',
            '/api/v1/namespaces/default/services/registry-mirror-hub',
            ''
        )
        responses.add(
            'PUT',
            '/api/v1/namespaces/default/services/registry-mirror-hub',
            ''
        )
        self.mirror.update_services(kubernetes.client.V1Service(
            metadata=self.mirror.metadata,
            spec=kubernetes.client.V1ServiceSpec()
        ), kubernetes.client.V1Service(
            metadata=self.mirror.metadata,
            spec=kubernetes.client.V1ServiceSpec()
        ))
        self.check_calls(('PUT', 'PUT'), responses.calls, self.mirror.metadata)

    @responses.activate
    def test_update_daemon_set_exists(self):
        '''Should replace 1 empty daemon set'''
        responses.add(
            'PUT',
            ('/apis/extensions/v1beta1/namespaces/default/daemonsets/registry'
             '-mirror-hub-utils'),
            ''
        )
        self.mirror.update_daemon_set(kubernetes.client.V1beta1DaemonSet())
        self.check_calls(('PUT',), responses.calls, self.mirror.metadata)

    @responses.activate
    def test_update_daemon_set_doesnt_exist(self):
        '''Should create then replace a daemon set'''
        responses.add(
            'POST',
            '/apis/extensions/v1beta1/namespaces/default/daemonsets',
            body=EMPTY_DAEMON_SET
        )
        self.mirror.update_daemon_set(None)
        self.check_calls(('POST',), responses.calls, self.mirror.metadata)

    @responses.activate
    def test_update_statefulset_exists(self):
        '''Should replace 1 empty stateful set'''
        responses.add(
            'PUT',
            ('/apis/apps/v1beta1/namespaces/default/statefulsets/registry'
             '-mirror-hub'),
            ''
        )
        self.mirror.update_stateful_set(kubernetes.client.V1beta1StatefulSet(
            spec=kubernetes.client.V1beta1StatefulSetSpec(

            )
        ))
        self.check_calls(('PUT',), responses.calls, self.mirror.metadata)

    @responses.activate
    def test_update_statefulset_doesnt_exist(self):
        '''Should create then replace a statefulset'''
        responses.add(
            'POST',
            '/apis/apps/v1beta1/namespaces/default/statefulsets',
            body=EMPTY_STATEFUL_SET
        )
        self.mirror.update_stateful_set(None)
        self.check_calls(('POST',), responses.calls, self.mirror.metadata)
