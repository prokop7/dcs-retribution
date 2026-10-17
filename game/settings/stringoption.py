from dataclasses import dataclass, field
from typing import Any, Optional

from .optiondescription import OptionDescription, SETTING_DESCRIPTION_KEY


@dataclass(frozen=True)
class StringOption(OptionDescription):
    multiline: bool
    placeholder: Optional[str]


def string_option(
    text: str,
    page: str,
    section: str,
    default: str,
    multiline: bool = False,
    placeholder: Optional[str] = None,
    detail: Optional[str] = None,
    tooltip: Optional[str] = None,
    causes_expensive_game_update: bool = False,
    **kwargs: Any,
) -> str:
    return field(
        metadata={
            SETTING_DESCRIPTION_KEY: StringOption(
                page,
                section,
                text,
                detail,
                tooltip,
                causes_expensive_game_update,
                multiline,
                placeholder,
            )
        },
        default=default,
        **kwargs,
    )
