from typing import Literal

from sentry_protos.snuba.v1.trace_item_attribute_pb2 import AttributeKey
from sentry_protos.snuba.v1.trace_item_filter_pb2 import ComparisonFilter

OPERATOR_MAP = {
    "=": ComparisonFilter.OP_EQUALS,
    "!=": ComparisonFilter.OP_NOT_EQUALS,
    "IN": ComparisonFilter.OP_IN,
    "NOT IN": ComparisonFilter.OP_NOT_IN,
    ">": ComparisonFilter.OP_GREATER_THAN,
    "<": ComparisonFilter.OP_LESS_THAN,
    ">=": ComparisonFilter.OP_GREATER_THAN_OR_EQUALS,
    "<=": ComparisonFilter.OP_LESS_THAN_OR_EQUALS,
}
IN_OPERATORS = ["IN", "NOT IN"]

SearchType = Literal[
    "byte",
    "duration",
    "integer",
    "millisecond",
    "number",
    "percentage",
    "string",
]

STRING = AttributeKey.TYPE_STRING
BOOLEAN = AttributeKey.TYPE_BOOLEAN
FLOAT = AttributeKey.TYPE_FLOAT
INT = AttributeKey.TYPE_INT

# TODO: we need a datetime type
# Maps search types back to types for the proto
TYPE_MAP: dict[SearchType, AttributeKey.Type.ValueType] = {
    "byte": FLOAT,
    "duration": FLOAT,
    "integer": INT,
    "millisecond": FLOAT,
    # TODO:  need to update these to float once the proto supports float arrays
    "number": INT,
    "percentage": FLOAT,
    "string": STRING,
}

# https://github.com/getsentry/snuba/blob/master/snuba/web/rpc/v1/endpoint_time_series.py
# The RPC limits us to 1000 points per timeseries
MAX_ROLLUP_POINTS = 1000
# Copied from snuba, a number of total seconds
VALID_GRANULARITIES = frozenset(
    {
        15,
        30,
        60,  # seconds
        2 * 60,
        5 * 60,
        10 * 60,
        30 * 60,  # minutes
        1 * 3600,
        3 * 3600,
        12 * 3600,
        24 * 3600,  # hours
    }
)
