#  Copyright 2022-2024 MTS (Mobile Telesystems)
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""Base model."""

import typing as tp
import warnings

import numpy as np
import pandas as pd

from rectools import Columns, ExternalIds, InternalIds
from rectools.dataset import Dataset
from rectools.dataset.identifiers import IdMap
from rectools.exceptions import NotFittedError
from rectools.types import ExternalIdsArray, InternalIdsArray

T = tp.TypeVar("T", bound="ModelBase")
ScoresArray = np.ndarray
Scores = tp.Union[tp.Sequence[float], ScoresArray]
ErrorBehaviour = tp.Literal["ignore", "warn", "raise"]

InternalRecoTriplet = tp.Tuple[InternalIds, InternalIds, Scores]
SemiInternalRecoTriplet = tp.Tuple[ExternalIds, InternalIds, Scores]
ExternalRecoTriplet = tp.Tuple[ExternalIds, ExternalIds, Scores]

RecoTriplet_T = tp.TypeVar("RecoTriplet_T", InternalRecoTriplet, SemiInternalRecoTriplet, ExternalRecoTriplet)


class ModelBase:
    """
    Base model class.

    Warning: This class should not be used directly.
    Use derived classes instead.
    """

    recommends_for_warm: bool = False
    recommends_for_cold: bool = False

    def __init__(self, *args: tp.Any, verbose: int = 0, **kwargs: tp.Any) -> None:
        self.is_fitted = False
        self.verbose = verbose

    def fit(self: T, dataset: Dataset, *args: tp.Any, **kwargs: tp.Any) -> T:
        """
        Fit model.

        Parameters
        ----------
        dataset : Dataset
            Dataset with input data.

        Returns
        -------
        self
        """
        self._fit(dataset, *args, **kwargs)
        self.is_fitted = True
        return self

    def _fit(self, dataset: Dataset, *args: tp.Any, **kwargs: tp.Any) -> None:
        raise NotImplementedError()

    def fit_partial(self: T, dataset: Dataset, *args: tp.Any, **kwargs: tp.Any) -> T:
        """
        Partial fit model.

        Parameters
        ----------
        dataset : Dataset
            Dataset with input data.

        Returns
        -------
        self
        """
        self._fit_partial(dataset, *args, **kwargs)
        self.is_fitted = True
        return self

    def _fit_partial(self, dataset: Dataset, *args: tp.Any, **kwargs: tp.Any) -> None:
        raise NotImplementedError()

    def _custom_transform_dataset_u2i(
        self, dataset: Dataset, users: ExternalIds, on_unsupported_targets: ErrorBehaviour
    ) -> Dataset:
        # This method should be overwritten for models that require dataset processing for u2i recommendations
        # E.g.: interactions filtering or changing mapping of internal ids based on model specific logic
        return dataset

    def _custom_transform_dataset_i2i(
        self, dataset: Dataset, target_items: ExternalIds, on_unsupported_targets: ErrorBehaviour
    ) -> Dataset:
        # This method should be overwritten for models that require dataset processing for i2i recommendations
        # E.g.: interactions filtering or changing mapping of internal ids based on model specific logic
        return dataset

    def recommend(
        self,
        users: ExternalIds,
        dataset: Dataset,
        k: int,
        filter_viewed: bool,
        items_to_recommend: tp.Optional[ExternalIds] = None,
        add_rank_col: bool = True,
        on_unsupported_targets: ErrorBehaviour = "raise",
    ) -> pd.DataFrame:
        r"""
        Recommend items for users.

        To use this method model must be fitted.

        Parameters
        ----------
        users : array-like
            Array of user ids to recommend for. User ids are supposed to be external
        dataset : Dataset
            Dataset with input data.
            Usually it's the same dataset that was used to fit model.
        k : int
            Derived number of recommendations for every user.
            Pay attention that in some cases real number of recommendations may be less than `k`.
        filter_viewed : bool
            Whether to filter from recommendations items that user has already interacted with.
            Works only for "hot" users.
        items_to_recommend : array-like, optional, default None
            Whitelist of item ids.
            If given, only these items will be used for recommendations.
            Otherwise all items from dataset will be used.
            Item ids are supposed to be external.
        add_rank_col : bool, default True
            Whether to add rank column to recommendations.
            If True column `Columns.Rank` will be added.
            This column contain integers from 1 to ``number of user recommendations``.
            In any case recommendations are sorted per rank for every user.
            The lesser the rank the more recommendation is relevant.
        on_unsupported_targets : Literal["raise", "warn", "ignore"], default "raise"
            How to handle warm/cold target users when model doesn't support warm/cold inference.
            Specify "raise" to raise ValueError in case unsupported targets are passed (default).
            Specify "ignore" to filter unsupported targets.
            Specify "warn" to filter with warning.

        Returns
        -------
        pd.DataFrame
            Recommendations table with columns `Columns.User`, `Columns.Item`, `Columns.Score`[, `Columns.Rank`].
            External user and item ids are used by default. For internal ids set `return_external_ids` to ``False``.
            1st column contains user ids,
            2nd - ids of recommended items sorted by relevance for each user,
            3rd - score that model gives for the user-item pair,
            4th (present only if `add_rank_col` is ``True``) - integers from ``1`` to number of user recommendations.

        Raises
        ------
        NotFittedError
            If called for not fitted model.
        TypeError, ValueError
            If arguments have inappropriate type or value
        ValueError
            If some of given users are warm/cold and model doesn't support such type of users and
            `on_unsupported_targets` is set to "raise".
        """
        self._check_is_fitted()
        self._check_k(k)

        dataset = self._custom_transform_dataset_u2i(dataset, users, on_unsupported_targets)

        sorted_item_ids_to_recommend = self._get_sorted_item_ids_to_recommend(items_to_recommend, dataset)

        # Here for hot and warm we get internal ids, for cold we keep external ids
        hot_user_ids, warm_user_ids, cold_user_ids = self._split_targets_by_hot_warm_cold(
            users,
            dataset,
            "user",
        )
        hot_user_ids, warm_user_ids, cold_user_ids = self._check_targets_are_valid(
            hot_user_ids, warm_user_ids, cold_user_ids, "user", on_unsupported_targets
        )

        reco_hot = self._init_internal_reco_triplet()
        reco_warm = self._init_internal_reco_triplet()
        reco_cold = self._init_semi_internal_reco_triplet()

        if hot_user_ids.size > 0:
            reco_hot = self._recommend_u2i(hot_user_ids, dataset, k, filter_viewed, sorted_item_ids_to_recommend)
        if warm_user_ids.size > 0:
            if self.recommends_for_warm:
                reco_warm = self._recommend_u2i_warm(warm_user_ids, dataset, k, sorted_item_ids_to_recommend)
            else:
                # TODO: use correct types for numpy arrays and stop ignoring
                reco_warm = self._recommend_cold(
                    warm_user_ids, dataset, k, sorted_item_ids_to_recommend
                )  # type: ignore
        if cold_user_ids.size > 0:
            reco_cold = self._recommend_cold(cold_user_ids, dataset, k, sorted_item_ids_to_recommend)

        reco_hot = self._adjust_reco_types(reco_hot)
        reco_warm = self._adjust_reco_types(reco_warm)
        reco_cold = self._adjust_reco_types(reco_cold, target_type=dataset.user_id_map.external_dtype)

        reco_hot_final = self._reco_to_external(reco_hot, dataset.user_id_map, dataset.item_id_map)
        reco_warm_final = self._reco_to_external(reco_warm, dataset.user_id_map, dataset.item_id_map)
        reco_cold_final = self._reco_items_to_external(reco_cold, dataset.item_id_map)

        del reco_hot, reco_warm, reco_cold

        reco_all = self._concat_reco((reco_hot_final, reco_warm_final, reco_cold_final))
        del reco_hot_final, reco_warm_final, reco_cold_final
        reco_df = self._make_reco_table(reco_all, Columns.User, add_rank_col)
        return reco_df

    def recommend_to_items(  # pylint: disable=too-many-branches
        self,
        target_items: ExternalIds,
        dataset: Dataset,
        k: int,
        filter_itself: bool = True,
        items_to_recommend: tp.Optional[ExternalIds] = None,
        add_rank_col: bool = True,
        on_unsupported_targets: ErrorBehaviour = "raise",
    ) -> pd.DataFrame:
        """
        Recommend items for target items.

        To use this method model must be fitted.

        Parameters
        ----------
        target_items : array-like
            Array of item ids to recommend for. Item ids are supposed to be external.
        dataset : Dataset
            Dataset with input data.
            Usually it's the same dataset that was used to fit model.
        k : int
            Derived number of recommendations for every target item.
            Pay attention that in some cases real number of recommendations may be less than `k`.
        filter_itself : bool, default True
            If True, item will be excluded from recommendations to itself.
        items_to_recommend : array-like, optional, default None
            Whitelist of item ids.
            If given, only these items will be used for recommendations.
            Otherwise all items from dataset will be used.
            Item ids are supposed to be external
        add_rank_col : bool, default True
             Whether to add rank column to recommendations.
             If True column `Columns.Rank` will be added.
             This column contain integers from 1 to ``number of item recommendations``.
             In any case recommendations are sorted per rank for every target item.
             Less rank means more relevant recommendation.
        on_unsupported_targets : Literal["raise", "warn", "ignore"], default "raise"
            How to handle warm/cold target users when model doesn't support warm/cold inference.
            Specify "raise" to raise ValueError in case unsupported targets are passed (default).
            Specify "ignore" to filter unsupported targets.
            Specify "warn" to filter with warning.

        Returns
        -------
        pd.DataFrame
            Recommendations table with columns `Columns.TargetItem`, `Columns.Item`, `Columns.Score`[, `Columns.Rank`].
            External item ids are used by default. For internal ids set `return_external_ids` to ``False``.
            1st column contains target item ids,
            2nd - ids of recommended items sorted by relevance for each target item,
            3rd - score that model gives for the target-item pair,
            4th (present only if `add_rank_col` is ``True``) - integers from 1 to number of recommendations.

        Raises
        ------
        NotFittedError
            If called for not fitted model.
        TypeError, ValueError
            If arguments have inappropriate type or value
        ValueError
            If some of given users are warm/cold and model doesn't support such type of users and
            `on_unsupported_targets` is set to "raise".
        """
        self._check_is_fitted()
        self._check_k(k)

        dataset = self._custom_transform_dataset_i2i(dataset, target_items, on_unsupported_targets)

        sorted_item_ids_to_recommend = self._get_sorted_item_ids_to_recommend(items_to_recommend, dataset)

        # Here for hot and warm we get internal ids, for cold we keep external ids
        hot_target_ids, warm_target_ids, cold_target_ids = self._split_targets_by_hot_warm_cold(
            target_items,
            dataset,
            "item",
        )
        hot_target_ids, warm_target_ids, cold_target_ids = self._check_targets_are_valid(
            hot_target_ids, warm_target_ids, cold_target_ids, "item", on_unsupported_targets
        )

        requested_k = k + 1 if filter_itself else k

        reco_hot = self._init_internal_reco_triplet()
        reco_warm = self._init_internal_reco_triplet()
        reco_cold = self._init_semi_internal_reco_triplet()

        if hot_target_ids.size > 0:
            reco_hot = self._recommend_i2i(hot_target_ids, dataset, requested_k, sorted_item_ids_to_recommend)
        if warm_target_ids.size > 0:
            if self.recommends_for_warm:
                reco_warm = self._recommend_i2i_warm(
                    warm_target_ids, dataset, requested_k, sorted_item_ids_to_recommend
                )
            else:
                # TODO: use correct types for numpy arrays and stop ignoring
                reco_warm = self._recommend_cold(
                    warm_target_ids, dataset, requested_k, sorted_item_ids_to_recommend
                )  # type: ignore
        if cold_target_ids.size > 0:
            # We intentionally request `k` and not `requested_k` here since we're not going to filter cold reco later
            reco_cold = self._recommend_cold(cold_target_ids, dataset, k, sorted_item_ids_to_recommend)

        reco_hot = self._adjust_reco_types(reco_hot)
        reco_warm = self._adjust_reco_types(reco_warm)
        reco_cold = self._adjust_reco_types(reco_cold, target_type=dataset.item_id_map.external_dtype)

        if filter_itself:
            reco_hot = self._filter_item_itself_from_i2i_reco(reco_hot, k)
            reco_warm = self._filter_item_itself_from_i2i_reco(reco_warm, k)
            # We don't filter cold reco since we never recommend cold items

        reco_hot_final = self._reco_to_external(reco_hot, dataset.item_id_map, dataset.item_id_map)
        reco_warm_final = self._reco_to_external(reco_warm, dataset.item_id_map, dataset.item_id_map)
        reco_cold_final = self._reco_items_to_external(reco_cold, dataset.item_id_map)
        del reco_hot, reco_warm, reco_cold

        reco_all = self._concat_reco((reco_hot_final, reco_warm_final, reco_cold_final))
        del reco_hot_final, reco_warm_final, reco_cold_final
        reco_df = self._make_reco_table(reco_all, Columns.TargetItem, add_rank_col)
        return reco_df

    def _check_is_fitted(self) -> None:
        if not self.is_fitted:
            raise NotFittedError(self.__class__.__name__)

    @classmethod
    def _check_k(cls, k: int) -> None:
        if k <= 0:
            raise ValueError("`k` must be positive integer")

    @classmethod
    def _init_semi_internal_reco_triplet(cls) -> SemiInternalRecoTriplet:
        return [], [], []

    @classmethod
    def _init_internal_reco_triplet(cls) -> InternalRecoTriplet:
        return [], [], []

    @classmethod
    def _get_sorted_item_ids_to_recommend(
        cls, items_to_recommend: tp.Optional[ExternalIds], dataset: Dataset
    ) -> tp.Optional[InternalIdsArray]:
        if items_to_recommend is None:
            return None

        internal_ids_to_recommend = dataset.item_id_map.convert_to_internal(items_to_recommend, strict=False)

        return np.unique(internal_ids_to_recommend)

    @classmethod
    def _split_targets_by_hot_warm_cold(
        cls,
        targets: ExternalIds,  # users for U2I or target items for I2I
        dataset: Dataset,
        entity: tp.Literal["user", "item"],
    ) -> tp.Tuple[InternalIdsArray, InternalIdsArray, ExternalIdsArray]:
        if entity == "user":
            id_map, n_hot = dataset.user_id_map, dataset.n_hot_users
        else:
            id_map, n_hot = dataset.item_id_map, dataset.n_hot_items

        known_ids, cold_ids = id_map.convert_to_internal(targets, strict=False, return_missing=True)
        try:
            cold_ids = cold_ids.astype(id_map.external_dtype)
        except ValueError:
            raise TypeError(
                f"Given {entity} ids must be convertible to the "
                f"{entity}_id` type in dataset ({id_map.external_dtype})"
            )

        hot_mask = known_ids < n_hot
        hot_ids = known_ids[hot_mask]
        warm_ids = known_ids[~hot_mask]
        return hot_ids, warm_ids, cold_ids

    @classmethod
    def _check_targets_are_valid(
        cls,
        hot_targets: InternalIdsArray,
        warm_targets: InternalIdsArray,
        cold_targets: ExternalIdsArray,
        entity: tp.Literal["user", "item"],
        on_unsupported_targets: ErrorBehaviour,
    ) -> tp.Tuple[InternalIdsArray, InternalIdsArray, ExternalIdsArray]:
        if warm_targets.size > 0 and not cls.recommends_for_warm and not cls.recommends_for_cold:
            explanation = f"""
                Model `{cls}` doesn't support recommendations for warm and cold {entity}s,
                but some of given {entity}s are warm: they are not in the interactions
            """
            if on_unsupported_targets == "warn":
                warnings.warn(explanation)
            elif on_unsupported_targets == "raise":
                raise ValueError(explanation)
            warm_targets = np.asarray([])

        if cold_targets.size > 0 and not cls.recommends_for_cold:
            explanation = f"""
                Model `{cls}` doesn't support recommendations for cold {entity}s,
                but some of given {entity}s are cold: they are not in the `dataset.{entity}_id_map`
            """
            if on_unsupported_targets == "warn":
                warnings.warn(explanation)
            elif on_unsupported_targets == "raise":
                raise ValueError(explanation)
            cold_targets = np.asarray([])
        return hot_targets, warm_targets, cold_targets

    @classmethod
    def _adjust_reco_types(cls, reco: RecoTriplet_T, target_type: tp.Type = np.int64) -> RecoTriplet_T:
        target_ids, item_ids, scores = reco
        target_ids = np.asarray(target_ids, dtype=target_type)
        item_ids = np.asarray(item_ids, dtype=np.int64)
        scores = np.asarray(scores, dtype=np.float32)
        return target_ids, item_ids, scores

    @classmethod
    def _filter_item_itself_from_i2i_reco(cls, reco: RecoTriplet_T, k: int) -> RecoTriplet_T:
        target_ids, item_ids, scores = reco
        df_reco = (
            pd.DataFrame({"tid": target_ids, "iid": item_ids, "score": scores})
            .query("tid != iid")
            .groupby("tid", sort=False)
            .head(k)
        )
        return df_reco["tid"].values, df_reco["iid"].values, df_reco["score"].values

    @classmethod
    def _reco_to_external(
        cls, reco: InternalRecoTriplet, target_id_map: IdMap, item_id_map: IdMap
    ) -> ExternalRecoTriplet:
        target_ids, item_ids, scores = reco
        target_ids = target_id_map.convert_to_external(target_ids)
        item_ids = item_id_map.convert_to_external(item_ids)
        return target_ids, item_ids, scores

    @classmethod
    def _reco_items_to_external(cls, reco: SemiInternalRecoTriplet, item_id_map: IdMap) -> ExternalRecoTriplet:
        target_ids, item_ids, scores = reco
        item_ids = item_id_map.convert_to_external(item_ids)
        return target_ids, item_ids, scores

    @classmethod
    def _concat_reco(cls, parts: tp.Sequence[RecoTriplet_T]) -> RecoTriplet_T:
        targets = np.concatenate([part[0] for part in parts])
        items = np.concatenate([part[1] for part in parts])
        scores = np.concatenate([part[2] for part in parts])
        return targets, items, scores

    @classmethod
    def _make_reco_table(cls, reco: ExternalRecoTriplet, target_col: str, add_rank_col: bool) -> pd.DataFrame:
        target_ids, item_ids, scores = reco
        df = pd.DataFrame(
            {
                target_col: target_ids,
                Columns.Item: item_ids,
                Columns.Score: scores,
            }
        )

        if add_rank_col:
            df[Columns.Rank] = df.groupby(target_col, sort=False).cumcount() + 1

        return df

    def _recommend_cold(
        self,
        target_ids: ExternalIdsArray,
        dataset: Dataset,
        k: int,
        sorted_item_ids_to_recommend: tp.Optional[InternalIdsArray],
    ) -> SemiInternalRecoTriplet:
        raise NotImplementedError()

    def _recommend_u2i_warm(
        self,
        user_ids: InternalIdsArray,
        dataset: Dataset,
        k: int,
        sorted_item_ids_to_recommend: tp.Optional[InternalIdsArray],
    ) -> InternalRecoTriplet:
        raise NotImplementedError()

    def _recommend_i2i_warm(
        self,
        target_ids: InternalIdsArray,
        dataset: Dataset,
        k: int,
        sorted_item_ids_to_recommend: tp.Optional[InternalIdsArray],
    ) -> InternalRecoTriplet:
        raise NotImplementedError()

    def _recommend_u2i(
        self,
        user_ids: InternalIdsArray,
        dataset: Dataset,
        k: int,
        filter_viewed: bool,
        sorted_item_ids_to_recommend: tp.Optional[InternalIdsArray],
    ) -> InternalRecoTriplet:
        raise NotImplementedError()

    def _recommend_i2i(
        self,
        target_ids: InternalIdsArray,
        dataset: Dataset,
        k: int,
        sorted_item_ids_to_recommend: tp.Optional[InternalIdsArray],
    ) -> InternalRecoTriplet:
        raise NotImplementedError()


class FixedColdRecoModelMixin:
    """
    Mixin for models that have fixed cold recommendations.

    Models that use this mixin should implement `_get_cold_reco` method.
    """

    def _recommend_cold(
        self,
        target_ids: ExternalIdsArray,
        dataset: Dataset,
        k: int,
        sorted_item_ids_to_recommend: tp.Optional[InternalIdsArray],
    ) -> SemiInternalRecoTriplet:
        item_ids, scores = self._get_cold_reco(dataset, k, sorted_item_ids_to_recommend)
        reco_target_ids = np.repeat(target_ids, len(item_ids))
        reco_item_ids = np.tile(item_ids, len(target_ids))
        reco_scores = np.tile(scores, len(target_ids))

        return reco_target_ids, reco_item_ids, reco_scores

    def _get_cold_reco(
        self, dataset: Dataset, k: int, sorted_item_ids_to_recommend: tp.Optional[InternalIdsArray]
    ) -> tp.Tuple[InternalIds, Scores]:
        raise NotImplementedError()
