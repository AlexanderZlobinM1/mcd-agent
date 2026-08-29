from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import state_push


def test_state_push_keeps_form_embed_status_in_observed_runtime() -> None:
    local_runtime = {"profile_name": "midi"}
    form_state = {"instances": {"example.sales-snap.com": {"status": "applied"}}}
    with (
        patch.object(state_push, "local_runtime_overrides", return_value=local_runtime),
        patch.object(state_push, "load_form_embed_state", return_value=form_state),
    ):
        observed = state_push._runtime_overrides_for_state_push(SimpleNamespace())

    assert observed == {
        "profile_name": "midi",
        "form_embed_instance_status": {"example.sales-snap.com": {"status": "applied"}},
    }
    assert local_runtime == {"profile_name": "midi"}


def test_state_push_clears_missing_form_embed_status() -> None:
    with (
        patch.object(state_push, "local_runtime_overrides", return_value={}),
        patch.object(state_push, "load_form_embed_state", return_value={"instances": "invalid"}),
    ):
        observed = state_push._runtime_overrides_for_state_push(SimpleNamespace())

    assert observed == {"form_embed_instance_status": {}}
