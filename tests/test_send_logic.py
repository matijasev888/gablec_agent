import pytest
import gablec_daily as gd


@pytest.mark.parametrize("ready,total,final,already_sent,expected", [
    # Already sent today -> always skip
    (3, 3, False, True, "skip_sent"),
    (0, 3, True, True, "skip_sent"),
    # Send #1 (not final): post only if ALL ready
    (3, 3, False, False, "post"),
    (2, 3, False, False, "defer"),
    (0, 3, False, False, "defer"),
    # Send #2 (final / deadline): post if >=1, skip if all empty
    (3, 3, True, False, "post"),
    (1, 3, True, False, "post"),
    (0, 3, True, False, "skip_empty"),
])
def test_decide_send_action(ready, total, final, already_sent, expected):
    assert gd.decide_send_action(ready, total, final, already_sent) == expected
