"""Job-board market selection: non-US ICPs must not silently search US Indeed."""

import pytest

from harvey.agents.scout import Scout


@pytest.mark.parametrize(
    "geo, sites, country",
    [
        ("Bengaluru, India", ["indeed", "naukri"], "india"),
        ("India", ["indeed", "naukri"], "india"),
        ("London, United Kingdom", ["indeed"], "united kingdom"),
        ("Denver, CO", ["indeed"], "usa"),
        ("United States", ["indeed"], "usa"),
        ("Austin", ["indeed"], "austin"),
        ("", ["indeed"], "usa"),
    ],
)
def test_job_board_market(geo, sites, country):
    assert Scout._job_board_market(geo) == (sites, country)
