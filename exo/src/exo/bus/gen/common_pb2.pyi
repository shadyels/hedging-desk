from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class InstrumentClass(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    INSTRUMENT_CLASS_UNSPECIFIED: _ClassVar[InstrumentClass]
    INSTRUMENT_CLASS_EQUITY: _ClassVar[InstrumentClass]
    INSTRUMENT_CLASS_INDEX: _ClassVar[InstrumentClass]
    INSTRUMENT_CLASS_ETF: _ClassVar[InstrumentClass]
    INSTRUMENT_CLASS_OPTION: _ClassVar[InstrumentClass]
    INSTRUMENT_CLASS_FORWARD: _ClassVar[InstrumentClass]
    INSTRUMENT_CLASS_FUTURE: _ClassVar[InstrumentClass]
    INSTRUMENT_CLASS_FX_SPOT: _ClassVar[InstrumentClass]
    INSTRUMENT_CLASS_FX_FORWARD: _ClassVar[InstrumentClass]
    INSTRUMENT_CLASS_BOND: _ClassVar[InstrumentClass]

class Side(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SIDE_UNSPECIFIED: _ClassVar[Side]
    SIDE_BUY: _ClassVar[Side]
    SIDE_SELL: _ClassVar[Side]
INSTRUMENT_CLASS_UNSPECIFIED: InstrumentClass
INSTRUMENT_CLASS_EQUITY: InstrumentClass
INSTRUMENT_CLASS_INDEX: InstrumentClass
INSTRUMENT_CLASS_ETF: InstrumentClass
INSTRUMENT_CLASS_OPTION: InstrumentClass
INSTRUMENT_CLASS_FORWARD: InstrumentClass
INSTRUMENT_CLASS_FUTURE: InstrumentClass
INSTRUMENT_CLASS_FX_SPOT: InstrumentClass
INSTRUMENT_CLASS_FX_FORWARD: InstrumentClass
INSTRUMENT_CLASS_BOND: InstrumentClass
SIDE_UNSPECIFIED: Side
SIDE_BUY: Side
SIDE_SELL: Side

class Meta(_message.Message):
    __slots__ = ("msg_id", "producer", "sent_ns", "schema_version")
    MSG_ID_FIELD_NUMBER: _ClassVar[int]
    PRODUCER_FIELD_NUMBER: _ClassVar[int]
    SENT_NS_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    msg_id: str
    producer: str
    sent_ns: int
    schema_version: int
    def __init__(self, msg_id: _Optional[str] = ..., producer: _Optional[str] = ..., sent_ns: _Optional[int] = ..., schema_version: _Optional[int] = ...) -> None: ...

class InstrumentRef(_message.Message):
    __slots__ = ("instrument_id", "symbol", "instrument_class", "currency")
    INSTRUMENT_ID_FIELD_NUMBER: _ClassVar[int]
    SYMBOL_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_CLASS_FIELD_NUMBER: _ClassVar[int]
    CURRENCY_FIELD_NUMBER: _ClassVar[int]
    instrument_id: int
    symbol: str
    instrument_class: InstrumentClass
    currency: str
    def __init__(self, instrument_id: _Optional[int] = ..., symbol: _Optional[str] = ..., instrument_class: _Optional[_Union[InstrumentClass, str]] = ..., currency: _Optional[str] = ...) -> None: ...

class ValuationMeta(_message.Message):
    __slots__ = ("model_id", "params_hash", "seed", "n_paths", "git_sha")
    MODEL_ID_FIELD_NUMBER: _ClassVar[int]
    PARAMS_HASH_FIELD_NUMBER: _ClassVar[int]
    SEED_FIELD_NUMBER: _ClassVar[int]
    N_PATHS_FIELD_NUMBER: _ClassVar[int]
    GIT_SHA_FIELD_NUMBER: _ClassVar[int]
    model_id: str
    params_hash: str
    seed: int
    n_paths: int
    git_sha: str
    def __init__(self, model_id: _Optional[str] = ..., params_hash: _Optional[str] = ..., seed: _Optional[int] = ..., n_paths: _Optional[int] = ..., git_sha: _Optional[str] = ...) -> None: ...
