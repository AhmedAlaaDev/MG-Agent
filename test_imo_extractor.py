from crm_output_formatter import MASTER_CARGO_KEY, records_to_master_json
from imo_extractor import extract_imo_fields
from validator import validate_and_correct


DG_FORM_TEXT = """
DANGEROUS GOODS: You must specify: UN No., proper shipping name/correct technical name,
IMO hazard class (subrisk), Packaging group, Marine pollutant, flashpoint.
Un no. : 3077
Proper shipping name : ENVIRONMENTALLY HAZARDOUS SUBSTANCE, SOLID, N.O.S.
Technical name (if have) : RUBBER ACCELERATOR MBT
Imo class : 9
Packing group : III
Marine pollutant (p,pp or no) : Y
Flash point (if have) :
Quantity of Package quantity : 200PACKAGES
"""


MSDS_TEXT = """
Section 14 TRANSPORT INFORMATION
UN number: 2283.
UN proper shipping name: ISOBUTYL METHACRYLATE, STABILIZED.
Transport hazard class(es): 3.
Packing group, if applicable: III.
Section 9 PHYSICAL AND CHEMICAL PROPERTIES
Flash point 45.5 C,
"""


def test_extract_imo_fields_from_dg_form_text():
    fields = extract_imo_fields(DG_FORM_TEXT)
    assert fields["_imo_detected"] is True
    assert fields["mesco_unnumber"] == "UN3077"
    assert fields["mesco_unno"] == "3077"
    assert fields["mesco_imoclass"] == "9"
    assert fields["mesco_marinepollutant"] is True
    assert "mesco_flashptc" not in fields


def test_validate_fills_msds_transport_imo_fields():
    rec = validate_and_correct(
        {
            "document_type": "Bill of Lading",
            "mesco_masterblno": "260039654",
            "mesco_cargodescription": "ISOBUTYL METHACRYLATE, STABILIZED",
        },
        MSDS_TEXT,
        enrichment_text=MSDS_TEXT,
    )
    assert rec["_imo_detected"] is True
    assert rec["mesco_unnumber"] == "UN2283"
    assert rec["mesco_unno"] == "2283"
    assert rec["mesco_imoclass"] == "3"
    assert rec["mesco_flashptc"] == "45.5"


def test_records_to_master_json_maps_imo_to_cargo_row():
    crm = records_to_master_json(
        [
            {
                "mesco_masterblno": "260082992",
                "mesco_cargodescription": "RUBBER ACCELERATOR MBT",
                "cr401_totalpackages": 200,
                "cr401_totalgrossweight": 5040,
                "_imo_detected": True,
                "mesco_imoclass": "9",
                "mesco_unnumber": "UN3077",
            }
        ]
    )
    cargo = crm[MASTER_CARGO_KEY][0]
    assert cargo["mesco_imo"] is True
    assert cargo["mesco_chemical"] is True
    assert cargo["mesco_imoclass"] == 9
    assert cargo["mesco_unno"] == "3077"
