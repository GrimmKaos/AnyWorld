"""Validated identifiers shared by inference requests and diagnostics."""

from enum import StrEnum


class RequestKind(StrEnum):
    ROUND = "round"
    DICE = "dice"
    TITLE = "title"
    INITIAL = "initial"
    CHANCE_RULE = "chance_rule"
    CHANCE_TRIGGER = "chance_trigger"
    SUMMARY = "summary"
    SUMMARY_AUDIT = "summary_audit"
    DICE_AUDIT = "dice_audit"
    EVENT_AUDIT = "event_audit"
