from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

REQUIRED_FIELDS = []


class ContainerItem(BaseModel):
    container_number: Optional[str] = None
    seal_number: Optional[str] = None
    container_type: Optional[str] = None
    packages: Optional[str] = None
    gross_weight_kg: Optional[str] = None
    measurement_cbm: Optional[str] = None


class BLEntity(BaseModel):
    document_type: Optional[str] = "Bill of Lading"
    mesco_masterblno: Optional[str] = None
    mesco_bookingnumber: Optional[str] = None
    mesco_acidnumber: Optional[str] = None

    mesco_shippernamecontactno: Optional[str] = None
    mesco_shipperaddress: Optional[str] = None
    mesco_consigneenamecontactno: Optional[str] = None
    mesco_consigneeaddress: Optional[str] = None
    mesco_notify1: Optional[str] = None
    mesco_notifyaddress: Optional[str] = None

    mesco_vessel: Optional[str] = None
    mesco_voytruckno: Optional[str] = None
    mesco_origin: Optional[str] = None
    mesco_destination: Optional[str] = None

    mesco_cargodescription: Optional[str] = None
    cr401_totalgrossweight: Optional[str] = None
    cr401_totalvolume: Optional[str] = None
    cr401_totalpackages: Optional[str] = None
    mesco_nooforgbls: Optional[str] = None

    mesco_containertype: Optional[str] = None
    mesco_containertype2: Optional[str] = None
    mesco_containertype3: Optional[str] = None
    mesco_handlinginformation: Optional[str] = None
    mesco_freightpayableat: Optional[str] = None
    mesco_ponumber: Optional[str] = None
    mesco_customerreference: Optional[str] = None
    mesco_bltype: Optional[int] = None
    mesco_transporttype: Optional[int] = None
    mesco_loadtype: Optional[int] = None
    mesco_direction: Optional[int] = None
    cr401_totalteus: Optional[str] = None

    mesco_pcfreightterm: Optional[str] = None
    mesco_etdorigin: Optional[str] = None
    mesco_etadestination: Optional[str] = None
    mesco_pickupaddress: Optional[str] = None
    mesco_deliveryaddress: Optional[str] = None
    mesco_transhipmentport: Optional[str] = None
    mesco_importerstaxno: Optional[str] = None
    mesco_foreignsupplierregistrationnumber: Optional[str] = None
    mesco_incoterm: Optional[str] = None
    mesco_telexrelease: Optional[bool] = False
    mesco_imoclass: Optional[str] = None
    mesco_unnumber: Optional[str] = None

    mesco_hscode: Optional[str] = None
    mesco_dateofissue: Optional[str] = None
    mesco_placeofissue: Optional[str] = None
    mesco_shippedonboarddate: Optional[str] = None

    container_number: Optional[str] = None
    seal_number: Optional[str] = None
    containers: List[ContainerItem] = Field(default_factory=list)

    extraction_method: Optional[str] = None
    extraction_quality: Dict[str, Any] = Field(default_factory=dict)
    confidence: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)

    class Config:
        extra = "allow"


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