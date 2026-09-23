import sys

import pytest

from bond_perpdex.__main__ import main
from bond_perpdex.models import UnknownOutcome
from bond_perpdex.offline import OfflineVenue


def test_no_network_no_transaction_smoke(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["bond-perpdex"])
    main()
    output = capsys.readouterr().out
    assert output == (
        "OFFLINE POST_ONLY BUY 0.002 BTCUSDCPERP @ 64939.90\n"
        "OFFLINE POST_ONLY SELL 0.002 BTCUSDCPERP @ 65070.10\n"
        "PASS: 2 signed quotes, confirmed cancellations, no open orders; "
        "zero network/transactions.\n"
    )
    assert "signature" not in output and "PRIVATE KEY" not in output


def test_shutdown_attempts_all_acknowledged_cancels_even_when_one_is_unknown(monkeypatch):
    venue = OfflineVenue()
    venue.fail_cancel = True
    monkeypatch.setattr("bond_perpdex.__main__.OfflineVenue", lambda: venue)
    monkeypatch.setattr(sys, "argv", ["bond-perpdex"])
    with pytest.raises(UnknownOutcome, match="2 orders"):
        main()
    assert sum(method == "DELETE" for method, _ in venue.calls) == 2
