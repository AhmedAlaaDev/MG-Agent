from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field


class ContainerItem(BaseModel):
    container_number: Optional[str] = None
    seal_number: Optional[str] = None
    container_type: Optional[str] = None
    packages: Optional[str] = None
    gross_weight_kg: Optional[str] = None
    measurement_cbm: Optional[str] = None


StrOrNum = Optional[Union[str, float, int]]


class BLEntity(BaseModel):
    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    document_type: Optional[str] = "Bill of Lading"
    mesco_masterblno: StrOrNum = None
    mesco_houseblno: StrOrNum = None
    mesco_bookingnumber: StrOrNum = None
    mesco_acidnumber: StrOrNum = None

    mesco_shippernamecontactno: StrOrNum = None
    mesco_shipperaddress: StrOrNum = None
    mesco_shippercontactnumber: StrOrNum = None
    mesco_consigneenamecontactno: StrOrNum = None
    mesco_consigneeaddress: StrOrNum = None
    mesco_notify1: StrOrNum = None
    mesco_notifyaddress: StrOrNum = None

    mesco_vessel: StrOrNum = None
    mesco_voytruckno: StrOrNum = None
    mesco_origin: StrOrNum = None
    mesco_destination: StrOrNum = None

    mesco_cargodescription: StrOrNum = None
    cr401_totalgrossweight: StrOrNum = None
    cr401_totalvolume: StrOrNum = None
    cr401_totalpackages: StrOrNum = None
    mesco_nooforgbls: StrOrNum = None

    mesco_containertype: StrOrNum = None
    mesco_containertype2: StrOrNum = None
    mesco_containertype3: StrOrNum = None
    mesco_handlinginformation: StrOrNum = None
    mesco_freightpayableat: StrOrNum = None
    mesco_ponumber: StrOrNum = None
    mesco_customerreference: StrOrNum = None
    mesco_bltype: Optional[int] = None
    mesco_transporttype: Optional[int] = None
    mesco_loadtype: Optional[int] = None
    mesco_direction: Optional[int] = None
    cr401_totalteus: StrOrNum = None

    mesco_pcfreightterm: StrOrNum = None
    mesco_etdorigin: StrOrNum = None
    mesco_etadestination: StrOrNum = None
    mesco_pickupaddress: StrOrNum = None
    mesco_deliveryaddress: StrOrNum = None
    mesco_routenotes: StrOrNum = None
    mesco_notes: StrOrNum = None
    mesco_certificatenumber: StrOrNum = None
    mesco_shippingline: StrOrNum = None
    mesco_transhipmentport: StrOrNum = None
    mesco_importerstaxno: StrOrNum = None
    mesco_foreignsupplierregistrationnumber: StrOrNum = None
    mesco_incoterm: StrOrNum = None
    mesco_telexrelease: Optional[bool] = False
    mesco_imoclass: StrOrNum = None
    mesco_unnumber: StrOrNum = None

    mesco_hscode: StrOrNum = None
    mesco_dateofissue: StrOrNum = None
    mesco_placeofissue: StrOrNum = None
    mesco_shippedonboarddate: StrOrNum = None

    container_number: StrOrNum = None
    seal_number: StrOrNum = None
    containers: List[ContainerItem] = Field(default_factory=list)

    extraction_method: Optional[str] = None
    extraction_quality: Dict[str, Any] = Field(default_factory=dict)
    confidence: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)


REQUIRED_FIELDS = list(BLEntity.model_fields.keys())


def empty_bl_entity() -> Dict[str, Any]:
    data = {k: None for k in REQUIRED_FIELDS}
    data["document_type"] = "Bill of Lading"
    data["containers"] = []
    data["mesco_telexrelease"] = False
    data["extraction_quality"] = {}
    data["confidence"] = {}
    data["warnings"] = []
    return data
