"""Reloj fijo: la suite sigue probando la campaña después de su vencimiento real."""
from datetime import timedelta
from unittest.mock import patch
from payment_policy import CAMPAIGN_START


def freeze_campaign():
    return patch('payment_policy.utc_now', return_value=CAMPAIGN_START + timedelta(hours=1))
