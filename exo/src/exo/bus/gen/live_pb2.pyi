import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class OrdStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ORD_STATUS_UNSPECIFIED: _ClassVar[OrdStatus]
    ORD_STATUS_NEW: _ClassVar[OrdStatus]
    ORD_STATUS_PARTIALLY_FILLED: _ClassVar[OrdStatus]
    ORD_STATUS_FILLED: _ClassVar[OrdStatus]
    ORD_STATUS_CANCELED: _ClassVar[OrdStatus]
    ORD_STATUS_REJECTED: _ClassVar[OrdStatus]
    ORD_STATUS_PENDING_CANCEL: _ClassVar[OrdStatus]
    ORD_STATUS_PENDING_REPLACE: _ClassVar[OrdStatus]
ORD_STATUS_UNSPECIFIED: OrdStatus
ORD_STATUS_NEW: OrdStatus
ORD_STATUS_PARTIALLY_FILLED: OrdStatus
ORD_STATUS_FILLED: OrdStatus
ORD_STATUS_CANCELED: OrdStatus
ORD_STATUS_REJECTED: OrdStatus
ORD_STATUS_PENDING_CANCEL: OrdStatus
ORD_STATUS_PENDING_REPLACE: OrdStatus

class TargetPosition(_message.Message):
    __slots__ = ("meta", "book_id", "instrument", "target_qty_e2", "as_of_ns", "band_qty_e2", "valuation", "reason")
    META_FIELD_NUMBER: _ClassVar[int]
    BOOK_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    TARGET_QTY_E2_FIELD_NUMBER: _ClassVar[int]
    AS_OF_NS_FIELD_NUMBER: _ClassVar[int]
    BAND_QTY_E2_FIELD_NUMBER: _ClassVar[int]
    VALUATION_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    meta: _common_pb2.Meta
    book_id: int
    instrument: _common_pb2.InstrumentRef
    target_qty_e2: int
    as_of_ns: int
    band_qty_e2: int
    valuation: _common_pb2.ValuationMeta
    reason: str
    def __init__(self, meta: _Optional[_Union[_common_pb2.Meta, _Mapping]] = ..., book_id: _Optional[int] = ..., instrument: _Optional[_Union[_common_pb2.InstrumentRef, _Mapping]] = ..., target_qty_e2: _Optional[int] = ..., as_of_ns: _Optional[int] = ..., band_qty_e2: _Optional[int] = ..., valuation: _Optional[_Union[_common_pb2.ValuationMeta, _Mapping]] = ..., reason: _Optional[str] = ...) -> None: ...

class ExecutionReport(_message.Message):
    __slots__ = ("meta", "cl_ord_id", "exec_id", "book_id", "instrument", "side", "status", "last_qty_e2", "last_px_e9", "cum_qty_e2", "leaves_qty_e2", "text")
    META_FIELD_NUMBER: _ClassVar[int]
    CL_ORD_ID_FIELD_NUMBER: _ClassVar[int]
    EXEC_ID_FIELD_NUMBER: _ClassVar[int]
    BOOK_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    SIDE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    LAST_QTY_E2_FIELD_NUMBER: _ClassVar[int]
    LAST_PX_E9_FIELD_NUMBER: _ClassVar[int]
    CUM_QTY_E2_FIELD_NUMBER: _ClassVar[int]
    LEAVES_QTY_E2_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    meta: _common_pb2.Meta
    cl_ord_id: str
    exec_id: str
    book_id: int
    instrument: _common_pb2.InstrumentRef
    side: _common_pb2.Side
    status: OrdStatus
    last_qty_e2: int
    last_px_e9: int
    cum_qty_e2: int
    leaves_qty_e2: int
    text: str
    def __init__(self, meta: _Optional[_Union[_common_pb2.Meta, _Mapping]] = ..., cl_ord_id: _Optional[str] = ..., exec_id: _Optional[str] = ..., book_id: _Optional[int] = ..., instrument: _Optional[_Union[_common_pb2.InstrumentRef, _Mapping]] = ..., side: _Optional[_Union[_common_pb2.Side, str]] = ..., status: _Optional[_Union[OrdStatus, str]] = ..., last_qty_e2: _Optional[int] = ..., last_px_e9: _Optional[int] = ..., cum_qty_e2: _Optional[int] = ..., leaves_qty_e2: _Optional[int] = ..., text: _Optional[str] = ...) -> None: ...

class PositionSnapshot(_message.Message):
    __slots__ = ("meta", "book_id", "lines")
    class Line(_message.Message):
        __slots__ = ("instrument", "qty_e2", "avg_px_e9", "inflight_qty_e2")
        INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
        QTY_E2_FIELD_NUMBER: _ClassVar[int]
        AVG_PX_E9_FIELD_NUMBER: _ClassVar[int]
        INFLIGHT_QTY_E2_FIELD_NUMBER: _ClassVar[int]
        instrument: _common_pb2.InstrumentRef
        qty_e2: int
        avg_px_e9: int
        inflight_qty_e2: int
        def __init__(self, instrument: _Optional[_Union[_common_pb2.InstrumentRef, _Mapping]] = ..., qty_e2: _Optional[int] = ..., avg_px_e9: _Optional[int] = ..., inflight_qty_e2: _Optional[int] = ...) -> None: ...
    META_FIELD_NUMBER: _ClassVar[int]
    BOOK_ID_FIELD_NUMBER: _ClassVar[int]
    LINES_FIELD_NUMBER: _ClassVar[int]
    meta: _common_pb2.Meta
    book_id: int
    lines: _containers.RepeatedCompositeFieldContainer[PositionSnapshot.Line]
    def __init__(self, meta: _Optional[_Union[_common_pb2.Meta, _Mapping]] = ..., book_id: _Optional[int] = ..., lines: _Optional[_Iterable[_Union[PositionSnapshot.Line, _Mapping]]] = ...) -> None: ...

class RiskSnapshot(_message.Message):
    __slots__ = ("meta", "greeks")
    class NetGreeks(_message.Message):
        __slots__ = ("underlying", "firm_delta_qty_e2", "notional_e9", "vega_e9", "gamma_qty_e2", "greeks_flagged", "rho_e9", "theta_e9", "div_sens_e9")
        UNDERLYING_FIELD_NUMBER: _ClassVar[int]
        FIRM_DELTA_QTY_E2_FIELD_NUMBER: _ClassVar[int]
        NOTIONAL_E9_FIELD_NUMBER: _ClassVar[int]
        VEGA_E9_FIELD_NUMBER: _ClassVar[int]
        GAMMA_QTY_E2_FIELD_NUMBER: _ClassVar[int]
        GREEKS_FLAGGED_FIELD_NUMBER: _ClassVar[int]
        RHO_E9_FIELD_NUMBER: _ClassVar[int]
        THETA_E9_FIELD_NUMBER: _ClassVar[int]
        DIV_SENS_E9_FIELD_NUMBER: _ClassVar[int]
        underlying: _common_pb2.InstrumentRef
        firm_delta_qty_e2: int
        notional_e9: int
        vega_e9: int
        gamma_qty_e2: int
        greeks_flagged: bool
        rho_e9: int
        theta_e9: int
        div_sens_e9: int
        def __init__(self, underlying: _Optional[_Union[_common_pb2.InstrumentRef, _Mapping]] = ..., firm_delta_qty_e2: _Optional[int] = ..., notional_e9: _Optional[int] = ..., vega_e9: _Optional[int] = ..., gamma_qty_e2: _Optional[int] = ..., greeks_flagged: bool = ..., rho_e9: _Optional[int] = ..., theta_e9: _Optional[int] = ..., div_sens_e9: _Optional[int] = ...) -> None: ...
    META_FIELD_NUMBER: _ClassVar[int]
    GREEKS_FIELD_NUMBER: _ClassVar[int]
    meta: _common_pb2.Meta
    greeks: _containers.RepeatedCompositeFieldContainer[RiskSnapshot.NetGreeks]
    def __init__(self, meta: _Optional[_Union[_common_pb2.Meta, _Mapping]] = ..., greeks: _Optional[_Iterable[_Union[RiskSnapshot.NetGreeks, _Mapping]]] = ...) -> None: ...

class RiskLimitAlert(_message.Message):
    __slots__ = ("meta", "underlying", "greek", "value_e9", "limit_e9", "breached")
    class Greek(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        GREEK_UNSPECIFIED: _ClassVar[RiskLimitAlert.Greek]
        GREEK_VEGA: _ClassVar[RiskLimitAlert.Greek]
        GREEK_GAMMA: _ClassVar[RiskLimitAlert.Greek]
        GREEK_RHO: _ClassVar[RiskLimitAlert.Greek]
    GREEK_UNSPECIFIED: RiskLimitAlert.Greek
    GREEK_VEGA: RiskLimitAlert.Greek
    GREEK_GAMMA: RiskLimitAlert.Greek
    GREEK_RHO: RiskLimitAlert.Greek
    META_FIELD_NUMBER: _ClassVar[int]
    UNDERLYING_FIELD_NUMBER: _ClassVar[int]
    GREEK_FIELD_NUMBER: _ClassVar[int]
    VALUE_E9_FIELD_NUMBER: _ClassVar[int]
    LIMIT_E9_FIELD_NUMBER: _ClassVar[int]
    BREACHED_FIELD_NUMBER: _ClassVar[int]
    meta: _common_pb2.Meta
    underlying: _common_pb2.InstrumentRef
    greek: RiskLimitAlert.Greek
    value_e9: int
    limit_e9: int
    breached: bool
    def __init__(self, meta: _Optional[_Union[_common_pb2.Meta, _Mapping]] = ..., underlying: _Optional[_Union[_common_pb2.InstrumentRef, _Mapping]] = ..., greek: _Optional[_Union[RiskLimitAlert.Greek, str]] = ..., value_e9: _Optional[int] = ..., limit_e9: _Optional[int] = ..., breached: bool = ...) -> None: ...

class InternalCrossNotice(_message.Message):
    __slots__ = ("meta", "cross_id", "instrument", "buy_book_id", "sell_book_id", "qty_e2", "ref_px_e9", "px_policy_id")
    META_FIELD_NUMBER: _ClassVar[int]
    CROSS_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    BUY_BOOK_ID_FIELD_NUMBER: _ClassVar[int]
    SELL_BOOK_ID_FIELD_NUMBER: _ClassVar[int]
    QTY_E2_FIELD_NUMBER: _ClassVar[int]
    REF_PX_E9_FIELD_NUMBER: _ClassVar[int]
    PX_POLICY_ID_FIELD_NUMBER: _ClassVar[int]
    meta: _common_pb2.Meta
    cross_id: str
    instrument: _common_pb2.InstrumentRef
    buy_book_id: int
    sell_book_id: int
    qty_e2: int
    ref_px_e9: int
    px_policy_id: str
    def __init__(self, meta: _Optional[_Union[_common_pb2.Meta, _Mapping]] = ..., cross_id: _Optional[str] = ..., instrument: _Optional[_Union[_common_pb2.InstrumentRef, _Mapping]] = ..., buy_book_id: _Optional[int] = ..., sell_book_id: _Optional[int] = ..., qty_e2: _Optional[int] = ..., ref_px_e9: _Optional[int] = ..., px_policy_id: _Optional[str] = ...) -> None: ...

class ReconAlert(_message.Message):
    __slots__ = ("meta", "book_id", "instrument", "exo_view_qty_e2", "d1_view_qty_e2", "publishing_halted")
    META_FIELD_NUMBER: _ClassVar[int]
    BOOK_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    EXO_VIEW_QTY_E2_FIELD_NUMBER: _ClassVar[int]
    D1_VIEW_QTY_E2_FIELD_NUMBER: _ClassVar[int]
    PUBLISHING_HALTED_FIELD_NUMBER: _ClassVar[int]
    meta: _common_pb2.Meta
    book_id: int
    instrument: _common_pb2.InstrumentRef
    exo_view_qty_e2: int
    d1_view_qty_e2: int
    publishing_halted: bool
    def __init__(self, meta: _Optional[_Union[_common_pb2.Meta, _Mapping]] = ..., book_id: _Optional[int] = ..., instrument: _Optional[_Union[_common_pb2.InstrumentRef, _Mapping]] = ..., exo_view_qty_e2: _Optional[int] = ..., d1_view_qty_e2: _Optional[int] = ..., publishing_halted: bool = ...) -> None: ...

class HealthStatus(_message.Message):
    __slots__ = ("meta", "component", "ok", "detail")
    class DetailEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    META_FIELD_NUMBER: _ClassVar[int]
    COMPONENT_FIELD_NUMBER: _ClassVar[int]
    OK_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    meta: _common_pb2.Meta
    component: str
    ok: bool
    detail: _containers.ScalarMap[str, str]
    def __init__(self, meta: _Optional[_Union[_common_pb2.Meta, _Mapping]] = ..., component: _Optional[str] = ..., ok: bool = ..., detail: _Optional[_Mapping[str, str]] = ...) -> None: ...

class TrackerAnalytics(_message.Message):
    __slots__ = ("meta", "book_id", "benchmark", "kind", "tracking_error_ann_e9", "tracking_diff_e9", "cash_weight_e9", "cash_drag_e9", "n_obs", "window_start_ns", "window_end_ns", "sampling_interval_s")
    class Kind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        KIND_UNSPECIFIED: _ClassVar[TrackerAnalytics.Kind]
        KIND_EX_POST: _ClassVar[TrackerAnalytics.Kind]
        KIND_EX_ANTE: _ClassVar[TrackerAnalytics.Kind]
    KIND_UNSPECIFIED: TrackerAnalytics.Kind
    KIND_EX_POST: TrackerAnalytics.Kind
    KIND_EX_ANTE: TrackerAnalytics.Kind
    META_FIELD_NUMBER: _ClassVar[int]
    BOOK_ID_FIELD_NUMBER: _ClassVar[int]
    BENCHMARK_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    TRACKING_ERROR_ANN_E9_FIELD_NUMBER: _ClassVar[int]
    TRACKING_DIFF_E9_FIELD_NUMBER: _ClassVar[int]
    CASH_WEIGHT_E9_FIELD_NUMBER: _ClassVar[int]
    CASH_DRAG_E9_FIELD_NUMBER: _ClassVar[int]
    N_OBS_FIELD_NUMBER: _ClassVar[int]
    WINDOW_START_NS_FIELD_NUMBER: _ClassVar[int]
    WINDOW_END_NS_FIELD_NUMBER: _ClassVar[int]
    SAMPLING_INTERVAL_S_FIELD_NUMBER: _ClassVar[int]
    meta: _common_pb2.Meta
    book_id: int
    benchmark: _common_pb2.InstrumentRef
    kind: TrackerAnalytics.Kind
    tracking_error_ann_e9: int
    tracking_diff_e9: int
    cash_weight_e9: int
    cash_drag_e9: int
    n_obs: int
    window_start_ns: int
    window_end_ns: int
    sampling_interval_s: int
    def __init__(self, meta: _Optional[_Union[_common_pb2.Meta, _Mapping]] = ..., book_id: _Optional[int] = ..., benchmark: _Optional[_Union[_common_pb2.InstrumentRef, _Mapping]] = ..., kind: _Optional[_Union[TrackerAnalytics.Kind, str]] = ..., tracking_error_ann_e9: _Optional[int] = ..., tracking_diff_e9: _Optional[int] = ..., cash_weight_e9: _Optional[int] = ..., cash_drag_e9: _Optional[int] = ..., n_obs: _Optional[int] = ..., window_start_ns: _Optional[int] = ..., window_end_ns: _Optional[int] = ..., sampling_interval_s: _Optional[int] = ...) -> None: ...

class ValuationSnapshot(_message.Message):
    __slots__ = ("meta", "book_id", "as_of_ns", "valuation", "lines", "book_pv_e9")
    class ProductLine(_message.Message):
        __slots__ = ("product_id", "product_type", "underlying", "pv_e9", "pv_std_err_e9", "delta_qty_e2", "vega_e9", "gamma_qty_e2", "rho_e9", "theta_e9", "div_sens_e9")
        PRODUCT_ID_FIELD_NUMBER: _ClassVar[int]
        PRODUCT_TYPE_FIELD_NUMBER: _ClassVar[int]
        UNDERLYING_FIELD_NUMBER: _ClassVar[int]
        PV_E9_FIELD_NUMBER: _ClassVar[int]
        PV_STD_ERR_E9_FIELD_NUMBER: _ClassVar[int]
        DELTA_QTY_E2_FIELD_NUMBER: _ClassVar[int]
        VEGA_E9_FIELD_NUMBER: _ClassVar[int]
        GAMMA_QTY_E2_FIELD_NUMBER: _ClassVar[int]
        RHO_E9_FIELD_NUMBER: _ClassVar[int]
        THETA_E9_FIELD_NUMBER: _ClassVar[int]
        DIV_SENS_E9_FIELD_NUMBER: _ClassVar[int]
        product_id: str
        product_type: str
        underlying: _common_pb2.InstrumentRef
        pv_e9: int
        pv_std_err_e9: int
        delta_qty_e2: int
        vega_e9: int
        gamma_qty_e2: int
        rho_e9: int
        theta_e9: int
        div_sens_e9: int
        def __init__(self, product_id: _Optional[str] = ..., product_type: _Optional[str] = ..., underlying: _Optional[_Union[_common_pb2.InstrumentRef, _Mapping]] = ..., pv_e9: _Optional[int] = ..., pv_std_err_e9: _Optional[int] = ..., delta_qty_e2: _Optional[int] = ..., vega_e9: _Optional[int] = ..., gamma_qty_e2: _Optional[int] = ..., rho_e9: _Optional[int] = ..., theta_e9: _Optional[int] = ..., div_sens_e9: _Optional[int] = ...) -> None: ...
    META_FIELD_NUMBER: _ClassVar[int]
    BOOK_ID_FIELD_NUMBER: _ClassVar[int]
    AS_OF_NS_FIELD_NUMBER: _ClassVar[int]
    VALUATION_FIELD_NUMBER: _ClassVar[int]
    LINES_FIELD_NUMBER: _ClassVar[int]
    BOOK_PV_E9_FIELD_NUMBER: _ClassVar[int]
    meta: _common_pb2.Meta
    book_id: int
    as_of_ns: int
    valuation: _common_pb2.ValuationMeta
    lines: _containers.RepeatedCompositeFieldContainer[ValuationSnapshot.ProductLine]
    book_pv_e9: int
    def __init__(self, meta: _Optional[_Union[_common_pb2.Meta, _Mapping]] = ..., book_id: _Optional[int] = ..., as_of_ns: _Optional[int] = ..., valuation: _Optional[_Union[_common_pb2.ValuationMeta, _Mapping]] = ..., lines: _Optional[_Iterable[_Union[ValuationSnapshot.ProductLine, _Mapping]]] = ..., book_pv_e9: _Optional[int] = ...) -> None: ...

class InternalTransferRequest(_message.Message):
    __slots__ = ("meta", "transfer_id", "instrument", "from_book_id", "to_book_id", "qty_e2", "reason", "valuation")
    META_FIELD_NUMBER: _ClassVar[int]
    TRANSFER_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    FROM_BOOK_ID_FIELD_NUMBER: _ClassVar[int]
    TO_BOOK_ID_FIELD_NUMBER: _ClassVar[int]
    QTY_E2_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    VALUATION_FIELD_NUMBER: _ClassVar[int]
    meta: _common_pb2.Meta
    transfer_id: str
    instrument: _common_pb2.InstrumentRef
    from_book_id: int
    to_book_id: int
    qty_e2: int
    reason: str
    valuation: _common_pb2.ValuationMeta
    def __init__(self, meta: _Optional[_Union[_common_pb2.Meta, _Mapping]] = ..., transfer_id: _Optional[str] = ..., instrument: _Optional[_Union[_common_pb2.InstrumentRef, _Mapping]] = ..., from_book_id: _Optional[int] = ..., to_book_id: _Optional[int] = ..., qty_e2: _Optional[int] = ..., reason: _Optional[str] = ..., valuation: _Optional[_Union[_common_pb2.ValuationMeta, _Mapping]] = ...) -> None: ...

class HedgeProposal(_message.Message):
    __slots__ = ("meta", "proposal_id", "book_id", "underlying", "target_greek", "legs", "pre_exposure_e9", "post_exposure_e9", "est_cost_e9", "valid_until_ns", "valuation")
    class Leg(_message.Message):
        __slots__ = ("instrument", "side", "qty_e2", "limit_px_e9")
        INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
        SIDE_FIELD_NUMBER: _ClassVar[int]
        QTY_E2_FIELD_NUMBER: _ClassVar[int]
        LIMIT_PX_E9_FIELD_NUMBER: _ClassVar[int]
        instrument: _common_pb2.InstrumentRef
        side: _common_pb2.Side
        qty_e2: int
        limit_px_e9: int
        def __init__(self, instrument: _Optional[_Union[_common_pb2.InstrumentRef, _Mapping]] = ..., side: _Optional[_Union[_common_pb2.Side, str]] = ..., qty_e2: _Optional[int] = ..., limit_px_e9: _Optional[int] = ...) -> None: ...
    META_FIELD_NUMBER: _ClassVar[int]
    PROPOSAL_ID_FIELD_NUMBER: _ClassVar[int]
    BOOK_ID_FIELD_NUMBER: _ClassVar[int]
    UNDERLYING_FIELD_NUMBER: _ClassVar[int]
    TARGET_GREEK_FIELD_NUMBER: _ClassVar[int]
    LEGS_FIELD_NUMBER: _ClassVar[int]
    PRE_EXPOSURE_E9_FIELD_NUMBER: _ClassVar[int]
    POST_EXPOSURE_E9_FIELD_NUMBER: _ClassVar[int]
    EST_COST_E9_FIELD_NUMBER: _ClassVar[int]
    VALID_UNTIL_NS_FIELD_NUMBER: _ClassVar[int]
    VALUATION_FIELD_NUMBER: _ClassVar[int]
    meta: _common_pb2.Meta
    proposal_id: str
    book_id: int
    underlying: _common_pb2.InstrumentRef
    target_greek: RiskLimitAlert.Greek
    legs: _containers.RepeatedCompositeFieldContainer[HedgeProposal.Leg]
    pre_exposure_e9: int
    post_exposure_e9: int
    est_cost_e9: int
    valid_until_ns: int
    valuation: _common_pb2.ValuationMeta
    def __init__(self, meta: _Optional[_Union[_common_pb2.Meta, _Mapping]] = ..., proposal_id: _Optional[str] = ..., book_id: _Optional[int] = ..., underlying: _Optional[_Union[_common_pb2.InstrumentRef, _Mapping]] = ..., target_greek: _Optional[_Union[RiskLimitAlert.Greek, str]] = ..., legs: _Optional[_Iterable[_Union[HedgeProposal.Leg, _Mapping]]] = ..., pre_exposure_e9: _Optional[int] = ..., post_exposure_e9: _Optional[int] = ..., est_cost_e9: _Optional[int] = ..., valid_until_ns: _Optional[int] = ..., valuation: _Optional[_Union[_common_pb2.ValuationMeta, _Mapping]] = ...) -> None: ...

class CommandRequest(_message.Message):
    __slots__ = ("meta", "manual_order", "kill_switch", "proposal_decision")
    class ProposalDecision(_message.Message):
        __slots__ = ("proposal_id", "approve", "operator", "reason")
        PROPOSAL_ID_FIELD_NUMBER: _ClassVar[int]
        APPROVE_FIELD_NUMBER: _ClassVar[int]
        OPERATOR_FIELD_NUMBER: _ClassVar[int]
        REASON_FIELD_NUMBER: _ClassVar[int]
        proposal_id: str
        approve: bool
        operator: str
        reason: str
        def __init__(self, proposal_id: _Optional[str] = ..., approve: bool = ..., operator: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...
    class ManualOrder(_message.Message):
        __slots__ = ("book_id", "instrument", "side", "qty_e2", "limit_px_e9")
        BOOK_ID_FIELD_NUMBER: _ClassVar[int]
        INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
        SIDE_FIELD_NUMBER: _ClassVar[int]
        QTY_E2_FIELD_NUMBER: _ClassVar[int]
        LIMIT_PX_E9_FIELD_NUMBER: _ClassVar[int]
        book_id: int
        instrument: _common_pb2.InstrumentRef
        side: _common_pb2.Side
        qty_e2: int
        limit_px_e9: int
        def __init__(self, book_id: _Optional[int] = ..., instrument: _Optional[_Union[_common_pb2.InstrumentRef, _Mapping]] = ..., side: _Optional[_Union[_common_pb2.Side, str]] = ..., qty_e2: _Optional[int] = ..., limit_px_e9: _Optional[int] = ...) -> None: ...
    class KillSwitch(_message.Message):
        __slots__ = ("engage", "operator")
        ENGAGE_FIELD_NUMBER: _ClassVar[int]
        OPERATOR_FIELD_NUMBER: _ClassVar[int]
        engage: bool
        operator: str
        def __init__(self, engage: bool = ..., operator: _Optional[str] = ...) -> None: ...
    META_FIELD_NUMBER: _ClassVar[int]
    MANUAL_ORDER_FIELD_NUMBER: _ClassVar[int]
    KILL_SWITCH_FIELD_NUMBER: _ClassVar[int]
    PROPOSAL_DECISION_FIELD_NUMBER: _ClassVar[int]
    meta: _common_pb2.Meta
    manual_order: CommandRequest.ManualOrder
    kill_switch: CommandRequest.KillSwitch
    proposal_decision: CommandRequest.ProposalDecision
    def __init__(self, meta: _Optional[_Union[_common_pb2.Meta, _Mapping]] = ..., manual_order: _Optional[_Union[CommandRequest.ManualOrder, _Mapping]] = ..., kill_switch: _Optional[_Union[CommandRequest.KillSwitch, _Mapping]] = ..., proposal_decision: _Optional[_Union[CommandRequest.ProposalDecision, _Mapping]] = ...) -> None: ...

class CommandAck(_message.Message):
    __slots__ = ("meta", "accepted", "reason", "cl_ord_id")
    META_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    CL_ORD_ID_FIELD_NUMBER: _ClassVar[int]
    meta: _common_pb2.Meta
    accepted: bool
    reason: str
    cl_ord_id: str
    def __init__(self, meta: _Optional[_Union[_common_pb2.Meta, _Mapping]] = ..., accepted: bool = ..., reason: _Optional[str] = ..., cl_ord_id: _Optional[str] = ...) -> None: ...

class Tick(_message.Message):
    __slots__ = ("meta", "instrument", "bid_px_e9", "ask_px_e9", "last_px_e9", "exch_ts_ns")
    META_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    BID_PX_E9_FIELD_NUMBER: _ClassVar[int]
    ASK_PX_E9_FIELD_NUMBER: _ClassVar[int]
    LAST_PX_E9_FIELD_NUMBER: _ClassVar[int]
    EXCH_TS_NS_FIELD_NUMBER: _ClassVar[int]
    meta: _common_pb2.Meta
    instrument: _common_pb2.InstrumentRef
    bid_px_e9: int
    ask_px_e9: int
    last_px_e9: int
    exch_ts_ns: int
    def __init__(self, meta: _Optional[_Union[_common_pb2.Meta, _Mapping]] = ..., instrument: _Optional[_Union[_common_pb2.InstrumentRef, _Mapping]] = ..., bid_px_e9: _Optional[int] = ..., ask_px_e9: _Optional[int] = ..., last_px_e9: _Optional[int] = ..., exch_ts_ns: _Optional[int] = ...) -> None: ...
