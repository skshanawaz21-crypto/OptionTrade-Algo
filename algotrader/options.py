from __future__ import annotations

import math


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _norm_pdf(value: float) -> float:
    return math.exp(-(value**2) / 2.0) / math.sqrt(2.0 * math.pi)


def black_scholes_greeks(
    spot_price: float,
    strike_price: float,
    time_to_expiry_years: float,
    risk_free_rate: float,
    volatility: float,
    option_type: str = "call",
) -> dict[str, float]:
    if time_to_expiry_years <= 0 or volatility <= 0 or spot_price <= 0 or strike_price <= 0:
        intrinsic = max(0.0, spot_price - strike_price)
        if option_type.lower() == "put":
            intrinsic = max(0.0, strike_price - spot_price)
        delta = 1.0 if spot_price > strike_price and option_type.lower() == "call" else 0.0
        if option_type.lower() == "put":
            delta = -1.0 if strike_price > spot_price else 0.0
        return {
            "price": intrinsic,
            "delta": delta,
            "gamma": 0.0,
            "theta": 0.0,
            "vega": 0.0,
            "rho": 0.0,
        }

    option_type = option_type.lower()
    d1 = (
        math.log(spot_price / strike_price)
        + (risk_free_rate + 0.5 * volatility**2) * time_to_expiry_years
    ) / (volatility * math.sqrt(time_to_expiry_years))
    d2 = d1 - volatility * math.sqrt(time_to_expiry_years)

    if option_type == "call":
        price = (
            spot_price * _norm_cdf(d1)
            - strike_price * math.exp(-risk_free_rate * time_to_expiry_years) * _norm_cdf(d2)
        )
        delta = _norm_cdf(d1)
        rho = strike_price * time_to_expiry_years * math.exp(-risk_free_rate * time_to_expiry_years) * _norm_cdf(d2)
    elif option_type == "put":
        price = (
            strike_price * math.exp(-risk_free_rate * time_to_expiry_years) * _norm_cdf(-d2)
            - spot_price * _norm_cdf(-d1)
        )
        delta = -_norm_cdf(-d1)
        rho = -strike_price * time_to_expiry_years * math.exp(-risk_free_rate * time_to_expiry_years) * _norm_cdf(-d2)
    else:
        raise ValueError("option_type must be 'call' or 'put'")

    gamma = _norm_pdf(d1) / (spot_price * volatility * math.sqrt(time_to_expiry_years))
    vega = (spot_price * _norm_pdf(d1) * math.sqrt(time_to_expiry_years)) / 100.0
    theta = (
        -spot_price * _norm_pdf(d1) * volatility / (2.0 * math.sqrt(time_to_expiry_years))
        - risk_free_rate
        * strike_price
        * math.exp(-risk_free_rate * time_to_expiry_years)
        * (_norm_cdf(d2) if option_type == "call" else _norm_cdf(-d2))
    ) / 365.0
    return {
        "price": price,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "rho": rho,
    }


def implied_volatility(
    market_price: float,
    spot_price: float,
    strike_price: float,
    time_to_expiry_years: float,
    risk_free_rate: float,
    option_type: str = "call",
    max_iter: int = 100,
    tolerance: float = 1e-5,
) -> float | None:
    sigma = 0.2
    for _ in range(max_iter):
        greeks = black_scholes_greeks(
            spot_price=spot_price,
            strike_price=strike_price,
            time_to_expiry_years=time_to_expiry_years,
            risk_free_rate=risk_free_rate,
            volatility=sigma,
            option_type=option_type,
        )
        model_price = greeks["price"]
        vega_raw = greeks["vega"] * 100.0
        if vega_raw < 1e-6:
            return None
        diff = model_price - market_price
        if abs(diff) < tolerance:
            return sigma
        sigma -= diff / vega_raw
        if sigma <= 0:
            sigma = 1e-4
    return sigma if sigma > 0 else None


def estimate_atm_strike(spot_price: float, step: int = 50) -> float:
    return round(spot_price / step) * step
