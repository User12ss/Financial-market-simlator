"""The market's hidden states: the estimate recovers parameters it was not told, and the filter follows the chain."""

from __future__ import annotations

import numpy as np
import pytest

from forecast.regime import RegimeModel, ahead, filtered, fit, simulate

TRUE = RegimeModel(
    means=[0.0006, -0.0008], vols=[0.007, 0.020],
    transition=[[0.98, 0.02], [0.05, 0.95]],
)


@pytest.fixture(scope="module")
def recovered():
    states, returns = simulate(TRUE, 2000, seed=20261122)
    return states, returns, fit(returns, states=2)


def test_two_states_are_recovered_from_2000_made_up_sessions(recovered):
    """The plan's own bar: every element of the matrix within 0.05, each state's volatility within 10%."""
    _, _, result = recovered
    found = result.model
    assert np.abs(np.array(found.transition) - np.array(TRUE.transition)).max() <= 0.05
    for estimated, true in zip(found.vols, TRUE.vols):
        assert abs(estimated / true - 1) <= 0.10


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_recovery_does_not_hang_on_one_lucky_history(seed):
    _, returns = simulate(TRUE, 2000, seed=seed)
    found = fit(returns, states=2).model
    assert np.abs(np.array(found.transition) - np.array(TRUE.transition)).max() <= 0.05
    assert all(abs(estimated / true - 1) <= 0.10 for estimated, true in zip(found.vols, TRUE.vols))


def test_states_are_numbered_by_volatility_the_calmest_first(recovered):
    _, _, result = recovered
    assert result.model.vols == sorted(result.model.vols)
    assert result.model.names == ["calm", "turbulent"]
    # The matrix, the last day's probabilities and the day-by-day ones are all in that order.
    states, _, _ = recovered
    turbulent = result.smoothed[:, 1] > 0.5
    assert (turbulent == (states == 1)).mean() > 0.9
    assert result.last[int(states[-1])] > 0.5


def test_each_row_of_the_matrix_and_each_day_sums_to_one(recovered):
    _, _, result = recovered
    assert np.allclose(np.array(result.model.transition).sum(axis=1), 1.0)
    assert np.allclose(result.smoothed.sum(axis=1), 1.0)
    assert sum(result.last) == pytest.approx(1.0)


def test_the_same_returns_give_the_same_estimate_to_the_last_digit(recovered):
    _, returns, result = recovered
    again = fit(returns, states=2)
    assert again.model == result.model
    assert again.last == result.last
    assert again.log_likelihood == result.log_likelihood


def test_more_starting_points_never_give_a_worse_likelihood(recovered):
    _, returns, result = recovered
    assert result.log_likelihood >= fit(returns, states=2, starts=1).log_likelihood - 1e-6


def test_two_states_explain_a_two_state_history_better_than_one(recovered):
    _, returns, result = recovered
    single = fit(returns, states=1)
    assert result.log_likelihood > single.log_likelihood + 50
    assert single.model.transition == [[1.0]]
    assert single.model.vols[0] == pytest.approx(float(returns.std()), rel=1e-6)
    assert single.model.means[0] == pytest.approx(float(returns.mean()), abs=1e-9)
    assert single.model.names == ["single"]


def test_the_filter_moves_to_the_turbulent_state_after_large_moves_and_back():
    calm = [0.001, -0.002, 0.0015, -0.001, 0.002, 0.0005]
    path = filtered(TRUE, calm + [-0.04, 0.035, -0.05] + calm * 6, prior=[0.5, 0.5])
    assert path[len(calm) - 1, 0] > 0.9          # quiet days: calm
    assert path[len(calm) + 2, 1] > 0.95         # three large moves: turbulent
    assert path[-1, 0] > 0.9                     # and calm again after weeks of quiet
    assert np.allclose(path.sum(axis=1), 1.0)


def test_filtering_in_two_parts_is_filtering_in_one():
    """What makes a week's parameters usable day by day: carry the last probabilities on, add the new returns."""
    _, returns = simulate(TRUE, 300, seed=7)
    whole = filtered(TRUE, returns, prior=[0.7, 0.3])
    first = filtered(TRUE, returns[:180], prior=[0.7, 0.3])
    second = filtered(TRUE, returns[180:], prior=first[-1])
    assert np.allclose(second[-1], whole[-1], atol=1e-12)
    assert np.allclose(second, whole[180:], atol=1e-12)


def test_the_last_day_of_an_estimate_is_where_the_filter_carries_on_from(recovered):
    _, returns, result = recovered
    more = np.array([0.001, -0.03, 0.02])
    carried = filtered(result.model, more, prior=result.last)
    assert carried.shape == (3, 2)
    assert filtered(result.model, [], prior=result.last).shape == (0, 2)


def test_looking_ahead_follows_the_matrix_and_ends_at_the_long_run_shares():
    assert ahead(TRUE, [1.0, 0.0], 0) == [1.0, 0.0]
    assert ahead(TRUE, [1.0, 0.0], 1) == pytest.approx([0.98, 0.02])
    assert ahead(TRUE, [0.0, 1.0], 2) == pytest.approx([0.05 * 0.98 + 0.95 * 0.05, 0.05 * 0.02 + 0.95 * 0.95])
    assert ahead(TRUE, [1.0, 0.0], 2000) == pytest.approx(TRUE.stationary(), abs=1e-9)
    assert TRUE.stationary() == pytest.approx([5 / 7, 2 / 7])
    with pytest.raises(ValueError, match="ahead"):
        ahead(TRUE, [1.0, 0.0], -1)


def test_a_return_no_state_can_explain_does_not_break_the_filter():
    path = filtered(TRUE, [0.001, 5.0, 0.001], prior=[0.9, 0.1])
    assert np.isfinite(path).all()
    assert np.allclose(path.sum(axis=1), 1.0)


def test_too_little_or_unusable_history_is_refused_in_words():
    with pytest.raises(ValueError, match="too few"):
        fit(np.full(100, 0.001) + np.arange(100) * 1e-6, states=2)
    with pytest.raises(ValueError, match="finite"):
        fit([0.01, float("nan")] * 200, states=2)
    with pytest.raises(ValueError, match="do not vary"):
        fit(np.zeros(500), states=2)
    with pytest.raises(ValueError, match="at least one state"):
        fit(np.arange(500) * 1e-5, states=0)


def test_three_states_are_named_and_ordered_too():
    model = RegimeModel(
        means=[0.0005, 0.0, -0.002], vols=[0.005, 0.012, 0.035],
        transition=[[0.97, 0.03, 0.0], [0.04, 0.94, 0.02], [0.0, 0.10, 0.90]],
    )
    _, returns = simulate(model, 4000, seed=11)
    found = fit(returns, states=3).model
    assert found.names == ["calm", "turbulent", "crisis"]
    assert found.vols == sorted(found.vols)
    assert found.vols[2] / found.vols[0] > 3


def test_a_state_that_rests_on_a_single_day_is_not_taken_for_a_regime():
    """One freak day can be 'explained' perfectly by a state of its own. That is a high likelihood and no regime."""
    rng = np.random.default_rng(5)
    returns = rng.normal(0.0, 0.01, 600)
    returns[300] = 0.5
    with pytest.raises(ValueError, match="No 2 states could be told apart: in every attempt one of them rested on fewer than 60 days"):
        fit(returns, states=2)
    # The same days with no freak among them are two states of much the same kind, each lived in for a long time.
    returns[300] = 0.01
    quiet = fit(returns, states=2)
    assert (quiet.smoothed.sum(axis=0) >= 60).all()
