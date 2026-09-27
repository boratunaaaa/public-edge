"""13D classification: reservation language is not activism; insiders / control holders are not activists."""
from datetime import date

from app.signals.thirteen_d import classify, insider_status, item4_features

BOILER = ("The Reporting Persons purchased the Shares based on the belief that the Shares were undervalued. No Reporting Person has any "
          "present plan or proposal which would relate to any of the matters set forth in subparagraphs (a) - (j) of Item 4. The Reporting "
          "Persons may in the future engage in communications with management and the Board regarding board composition, a sale of the "
          "Company, strategic alternatives and capital allocation.")
JANA = ("The Reporting Persons acquired the Shares because they believe the Shares are undervalued. JANA has had, and intends to continue "
        "to have, constructive discussions with members of the Issuer's board of directors and management team regarding: (i) management, "
        "(ii) compensation, (iii) corporate governance and board composition, and (iv) evaluating the Issuer's value in a sale transaction.")
PROXY = ("On March 3, 2025, the Reporting Persons delivered a letter to the Issuer nominating three highly qualified director candidates "
         "for election at the 2025 annual meeting. The Reporting Persons intend to file a preliminary proxy statement to solicit proxies.")
TECH = "This Amendment No. 4 is being filed solely to report a decrease in the percentage owned as a result of an increase in the number of outstanding shares."


def test_reservation_language_is_not_an_activist_flag():
    f = item4_features(BOILER)
    assert not any(f[k] for k in ("board_change", "sale_request", "strategic_alternatives", "capital_allocation"))
    assert f["hedged_mentions"] and classify(f)[0] != "ACTIVIST_13D"


def test_stated_engagement_and_proxy_are_activist():
    f = item4_features(JANA)
    assert f["board_change"] and f["governance_change"] and f["sale_request"] and f["management_change"]
    assert classify(f) [:2] == ("ACTIVIST_13D", "HIGH")
    p = item4_features(PROXY)
    assert p["proxy_intent"] and p["board_change"] and classify(p)[:2] == ("ACTIVIST_13D", "HIGH")


def test_technical_amendment_is_structural():
    f = item4_features(TECH, is_amendment=True)
    assert f["technical_amendment"] and classify(f, is_amendment=True)[0] == "PASSIVE_OR_STRUCTURAL_13D"


def test_insider_control_and_record_rules():
    f = item4_features(BOILER)
    assert classify(f, insider={"current_officer_director": True})[0] == "INSIDER_13D"
    assert classify(f, percent=62.0)[:2] == ("CONTROL_HOLDER_13D", "HIGH")
    assert classify(f, insider={"current_officer_director": True}, percent=25.0)[0] == "CONTROL_HOLDER_13D"  # founder
    assert classify(f, names=["Smith Family Trust"], insider={"linked_to_insider": True})[0] == "CONTROL_HOLDER_13D"
    assert classify(f, prior_campaigns=3)[:2] == ("ACTIVIST_13D", "LOW")  # a record without a stated demand never counts
    p = item4_features(PROXY)
    assert classify(p, insider={"current_officer_director": True})[:2] == ("AMBIGUOUS_13D", "LOW")  # insider with demands
    assert classify(p, insider={"current_officer_director": True}, prior_campaigns=3)[0] == "ACTIVIST_13D"  # activist on the board


def test_insider_status_is_point_in_time():
    filed = date(2024, 6, 1)
    assert insider_status([(date(2024, 1, 5), True, False, False)], filed)["current_officer_director"]
    assert insider_status([(date(2021, 1, 5), False, True, False)], filed)["former"]  # a former executive is not a current insider
    assert insider_status([(date(2024, 5, 1), False, False, True)], filed)["ten_pct_only"]  # a 10% owner alone is not an insider
    assert not insider_status([(date(2025, 3, 1), True, False, False)], filed)["current_officer_director"]  # later role: no look-ahead


def test_support_for_management_and_deal_rights_are_not_activism():
    sup = item4_features("C. Johnson acquired the Shares for investment and intends to support the re-election of the incumbent members "
                         "of the Issuer's board of trustees, who would be opposed by the dissident nominees.")
    assert not sup["board_change"]
    deal = item4_features("Pursuant to the Investor Rights Agreement, the Reporting Person entered into a cooperation agreement and has the "
                          "right to designate one director; the Reporting Person intends to nominate its designee to the board.")
    assert classify(deal)[0] != "ACTIVIST_13D"


def test_founder_family_shares_the_issuer_name():
    f = item4_features(BOILER)
    assert classify(f, names=["Estate of Bruce A. Nordstrom"], issuer_name="NORDSTROM INC")[0] == "CONTROL_HOLDER_13D"
    assert classify(f, names=["Starboard Value LP"], issuer_name="NORDSTROM INC")[0] != "CONTROL_HOLDER_13D"


def test_control_acquisitions_are_not_activism():
    f = item4_features("The Reporting Persons acquired the Ordinary Shares for the purpose of acquiring control of the Issuer. The Reporting "
                       "Persons intend to nominate directors to the board.")
    assert classify(f, percent=45.9)[0] == "CONTROL_HOLDER_13D"
    g = item4_features("In connection with the Offtake Agreement, the Reporting Persons have the right to appoint one director and intend to "
                       "nominate a director to the board.")
    assert classify(g, percent=9.9)[0] != "ACTIVIST_13D"
    assert classify(item4_features(PROXY_TXT), percent=35.0, prior_campaigns=5)[0] == "ACTIVIST_13D"  # an activist's large stake stays activist


PROXY_TXT = PROXY
