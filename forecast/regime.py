"""The market's hidden state: a Markov chain over a few regimes, each with its own daily return.

The market is taken to be in one of a few states that cannot be observed
(calm, turbulent, and with three states, crisis). Each has its own normal
distribution of daily log returns, and the state changes from one day to the
next according to a transition matrix. All of it is estimated from the
index's daily returns alone.

    fit        estimates the states and the matrix (EM, from fixed starting points)
    filtered   the probability of each state on each day, from the returns up to that day
    ahead      the state probabilities some sessions on

The states are numbered by volatility, the calmest first, so that "state 0"
means the same thing in every estimate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from pydantic import BaseModel, Field

STATE_NAMES = {1: ["single"], 2: ["calm", "turbulent"], 3: ["calm", "turbulent", "crisis"]}
# Fewer returns than this per state and the estimate says nothing.
MIN_RETURNS_PER_STATE = 60
# A state's volatility is never estimated below this share of the whole sample's: a state
# that closes in on a single day's return would have a likelihood without bound.
VOL_FLOOR = 0.05
_SQRT_2PI = math.sqrt(2 * math.pi)


class RegimeModel(BaseModel):
    """Estimated states, calmest first. `transition[i][j]` is the chance of going from state i to state j in a day."""

    means: list[float] = Field(description="Mean daily log return in each state")
    vols: list[float] = Field(description="Standard deviation of the daily log return in each state")
    transition: list[list[float]]

    @property
    def states(self) -> int:
        return len(self.vols)

    @property
    def names(self) -> list[str]:
        return STATE_NAMES.get(self.states) or [f"state {index + 1}" for index in range(self.states)]

    def matrix(self) -> np.ndarray:
        return np.array(self.transition, dtype=float)

    def stationary(self) -> list[float]:
        """The share of days spent in each state in the long run."""
        values, vectors = np.linalg.eig(self.matrix().T)
        vector = np.real(vectors[:, int(np.argmin(np.abs(values - 1.0)))])
        return [float(v) for v in vector / vector.sum()]


@dataclass
class Fit:
    model: RegimeModel
    last: list[float]              # probability of each state on the last day, from the returns up to it
    smoothed: np.ndarray           # the same for every day, from the whole sample: days x states
    log_likelihood: float
    iterations: int


def _densities(returns: np.ndarray, means: np.ndarray, vols: np.ndarray) -> np.ndarray:
    """Each day's return under each state's distribution. `means` and `vols` end in a states axis."""
    z = (returns[..., :, None] - means[..., None, :]) / vols[..., None, :]
    return np.exp(-0.5 * z * z) / (vols[..., None, :] * _SQRT_2PI)


def _forward(density: np.ndarray, transition: np.ndarray, first: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Filtered state probabilities for every day, and each day's likelihood given the days before.

    Works on several models at once: `density` is models x days x states,
    `transition` models x states x states, and `first` the models' state
    probabilities for the first day before its return is seen.
    """
    models, days, states = density.shape
    alpha = np.empty((models, days, states))
    scale = np.empty((models, days))
    predicted = first
    for day in range(days):
        joint = predicted * density[:, day, :]
        total = joint.sum(axis=1)
        # A return no state can account for (far out in every tail) leaves nothing to
        # normalise by. The day then says nothing about the state: the probabilities
        # stay where the chain alone puts them, and the day counts as all but impossible.
        dead = ~(total > 0)
        if dead.any():
            joint[dead] = predicted[dead]
            total = np.where(dead, joint.sum(axis=1), total)
        alpha[:, day, :] = joint / total[:, None]
        scale[:, day] = np.where(dead, 1e-300, total)
        predicted = np.einsum("mi,mij->mj", alpha[:, day, :], transition)
    return alpha, scale


def _backward(density: np.ndarray, transition: np.ndarray, scale: np.ndarray) -> np.ndarray:
    models, days, states = density.shape
    beta = np.ones((models, days, states))
    for day in range(days - 2, -1, -1):
        weighted = density[:, day + 1, :] * beta[:, day + 1, :] / scale[:, day + 1, None]
        beta[:, day, :] = np.einsum("mij,mj->mi", transition, weighted)
    return beta


def _starts(returns: np.ndarray, states: int, starts: int, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Where the estimate starts from. The same for the same seed: the first plain, the rest shaken."""
    rng = np.random.default_rng(seed)
    mean, vol = float(returns.mean()), float(returns.std())
    spread = np.geomspace(0.5, 2.5, states) if states > 1 else np.ones(1)
    means = np.full((starts, states), mean)
    vols = np.empty((starts, states))
    transition = np.empty((starts, states, states))
    for index in range(starts):
        shake = np.ones(states) if index == 0 else rng.uniform(0.7, 1.4, states)
        stay = np.full(states, 0.95) if index == 0 else rng.uniform(0.85, 0.99, states)
        vols[index] = vol * spread * shake
        for state in range(states):
            transition[index, state] = (1 - stay[state]) / (states - 1) if states > 1 else 1.0
            transition[index, state, state] = stay[state] if states > 1 else 1.0
    return means, vols, transition


def fit(
    returns, states: int = 2, starts: int = 6, seed: int = 0, max_iterations: int = 300, tolerance: float = 1e-6,
) -> Fit:
    """Estimate the states from daily log returns, oldest first.

    EM from `starts` fixed starting points at once; the one with the highest
    likelihood is kept. The same returns and seed give the same estimate.
    """
    returns = np.asarray(returns, dtype=float)
    if states < 1:
        raise ValueError("A regime model needs at least one state.")
    if returns.ndim != 1 or not np.isfinite(returns).all():
        raise ValueError("The returns have to be one series of finite numbers.")
    if len(returns) < MIN_RETURNS_PER_STATE * states:
        raise ValueError(
            f"{len(returns)} daily returns are too few to estimate {states} state(s): "
            f"at least {MIN_RETURNS_PER_STATE * states} are needed."
        )
    if float(returns.std()) <= 0:
        raise ValueError("The returns do not vary, so there is nothing to estimate.")

    starts = max(1, starts) if states > 1 else 1
    means, vols, transition = _starts(returns, states, starts, seed)
    first = np.full((starts, states), 1.0 / states)
    floor = (VOL_FLOOR * float(returns.std())) ** 2
    data = np.broadcast_to(returns, (starts, len(returns)))
    previous = np.full(starts, -np.inf)
    iterations = 0
    for iterations in range(1, max_iterations + 1):
        density = _densities(data, means, vols)
        alpha, scale = _forward(density, transition, first)
        beta = _backward(density, transition, scale)
        likelihood = np.log(scale).sum(axis=1)
        gamma = alpha * beta
        gamma /= gamma.sum(axis=2, keepdims=True)
        if states > 1:
            weighted = density[:, 1:, :] * beta[:, 1:, :] / scale[:, 1:, None]
            moves = transition * np.einsum("mti,mtj->mij", alpha[:, :-1, :], weighted)
            transition = moves / moves.sum(axis=2, keepdims=True)
        weight = gamma.sum(axis=1)
        means = np.einsum("mtk,t->mk", gamma, returns) / weight
        spread = np.einsum("mtk,mtk->mk", gamma, (data[:, :, None] - means[:, None, :]) ** 2) / weight
        vols = np.sqrt(np.maximum(spread, floor))
        first = gamma[:, 0, :]
        if np.all(likelihood - previous < tolerance):
            break
        previous = likelihood

    # The parameters just updated are one step past the likelihood in hand: measure them as they are.
    density = _densities(data, means, vols)
    alpha, scale = _forward(density, transition, first)
    beta = _backward(density, transition, scale)
    likelihood = np.log(scale).sum(axis=1)
    occupied = alpha * beta
    occupied /= occupied.sum(axis=2, keepdims=True)
    # A state can close in on a few extreme days and score a high likelihood by describing them
    # alone. That is not a regime the market spends time in: such an attempt is not taken.
    sound = (occupied.sum(axis=1) >= MIN_RETURNS_PER_STATE).all(axis=1)
    if not sound.any():
        raise ValueError(
            f"No {states} states could be told apart: in every attempt one of them rested on fewer than "
            f"{MIN_RETURNS_PER_STATE} days."
        )
    best = int(np.argmax(np.where(sound, likelihood, -np.inf)))
    order = np.argsort(vols[best], kind="stable")
    gamma = occupied[best]
    model = RegimeModel(
        means=[float(v) for v in means[best][order]],
        vols=[float(v) for v in vols[best][order]],
        transition=[[float(v) for v in row[order]] for row in transition[best][order]],
    )
    return Fit(
        model=model, last=[float(v) for v in alpha[best, -1, order]], smoothed=gamma[:, order],
        log_likelihood=float(likelihood[best]), iterations=iterations,
    )


def filtered(model: RegimeModel, returns, prior) -> np.ndarray:
    """The probability of each state on each day, given the returns up to that day.

    `prior` is the state probabilities on the day before the first return. The
    result has one row per return; with no returns it is empty, and the prior
    still stands.
    """
    returns = np.asarray(returns, dtype=float)
    prior = np.asarray(prior, dtype=float)
    if len(returns) == 0:
        return np.empty((0, model.states))
    matrix = model.matrix()
    density = _densities(returns[None, :], np.array(model.means)[None, :], np.array(model.vols)[None, :])
    alpha, _ = _forward(density, matrix[None, :, :], (prior @ matrix)[None, :])
    return alpha[0]


def ahead(model: RegimeModel, now, sessions: int) -> list[float]:
    """The state probabilities that many sessions after a day whose probabilities are `now`."""
    if sessions < 0:
        raise ValueError("A forecast looks ahead, not back.")
    result = np.asarray(now, dtype=float) @ np.linalg.matrix_power(model.matrix(), sessions)
    return [float(v) for v in result]


def simulate(model: RegimeModel, days: int, seed: int, start: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """A made-up history from known parameters: the states and the returns. For checking the estimate."""
    rng = np.random.default_rng(seed)
    matrix = np.cumsum(model.matrix(), axis=1)
    states = np.empty(days, dtype=int)
    state = start
    for day in range(days):
        states[day] = state
        state = min(int(np.searchsorted(matrix[state], rng.random(), side="right")), model.states - 1)
    returns = np.array(model.means)[states] + np.array(model.vols)[states] * rng.standard_normal(days)
    return states, returns
