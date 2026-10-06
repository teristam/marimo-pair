from dataclasses import dataclass


@dataclass
class BranchModel:
    label: str
    values: list[int]
