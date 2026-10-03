# Stubs for the part of catboost that oblako's example containers use; catboost
# is installed only inside those containers, never in oblako's environment.
from typing import Any

class Pool:
    def __init__(
        self,
        data: Any,
        label: Any = ...,
        cat_features: list[int] | list[str] | None = ...,
    ) -> None: ...

class CatBoostClassifier:
    def __init__(self, **params: Any) -> None: ...
    def fit(self, X: Any, y: Any = ..., **kwargs: Any) -> CatBoostClassifier: ...
    def predict_proba(self, data: Any) -> Any: ...
    def get_feature_importance(self, data: Any = ..., type: str = ...) -> Any: ...
