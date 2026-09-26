import importlib

import pytest
from pydantic import ValidationError

from autobuild_json.gateway.identity.policy import KeyPolicy


def test_capabilities_are_fixed_and_caller_mutation_cannot_disable_transports():
    module = importlib.import_module('autobuild_json.gateway.providers.codex_capabilities')
    value = module.codex_capabilities()
    assert value['version'] == 'codex-http-v2'
    assert value['compact'] is True and value['images'] is True and value['websocket'] is True
    value['websocket'] = False
    assert module.codex_capabilities()['websocket'] is True


def test_policy_defaults_preserve_existing_namespace():
    policy = KeyPolicy()
    assert policy.model_prefix == '' and policy.excluded_model_ids == frozenset()
    assert KeyPolicy(model_prefix='team/', excluded_model_ids={'blocked'}).model_prefix == 'team/'


@pytest.mark.parametrize('prefix', ['*', 'team/*', '?', ' team/', 'team/\n', 'é/', 'a'*65, '../', 'a/../', 'a//'])
def test_unsafe_or_ambiguous_prefix_is_rejected(prefix):
    with pytest.raises(ValidationError):
        KeyPolicy(model_prefix=prefix)
