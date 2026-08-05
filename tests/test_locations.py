from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from app.config import Settings
from app.services.locations.geocoding import (
    enforce_geocoding_rate_limit,
    reverse_geocode,
    search_places,
    validate_search_query,
)
from app.services.locations.model import (
    create_location,
    is_map_eligible,
    serialize_location,
    validate_location_input,
)


def test_validate_location_requires_coords_when_offline() -> None:
    with pytest.raises(HTTPException) as exc:
        validate_location_input(
            {
                "display_label": "Town Hall",
                "precision": "approximate",
                "is_online": False,
            }
        )
    assert exc.value.detail == "location_coordinates_required"


def test_validate_location_rejects_invalid_precision() -> None:
    with pytest.raises(HTTPException) as exc:
        validate_location_input(
            {
                "display_label": "Town Hall",
                "latitude": 1.0,
                "longitude": 2.0,
                "precision": "street",
            }
        )
    assert exc.value.detail == "invalid_location_precision"


def test_validate_location_defaults_to_approximate() -> None:
    result = validate_location_input(
        {
            "display_label": "Town Hall",
            "latitude": 51.507351,
            "longitude": -0.127758,
        }
    )
    assert result["precision"] == "approximate"
    assert result["is_online"] is False
    assert result["latitude"] == 51.507351
    assert result["longitude"] == -0.127758


def test_validate_location_online_skips_coordinates() -> None:
    result = validate_location_input(
        {
            "display_label": "Online meetup",
            "is_online": True,
            "precision": "exact",
        }
    )
    assert result["is_online"] is True
    assert result["latitude"] is None
    assert result["longitude"] is None


def test_serialize_approximate_coarsens_coordinates() -> None:
    row = {
        "id": uuid4(),
        "provider_place_id": "nominatim:1",
        "display_label": "Market Square",
        "latitude": Decimal("51.507351"),
        "longitude": Decimal("-0.127758"),
        "region": "London",
        "country": "United Kingdom",
        "precision": "approximate",
        "is_online": False,
    }
    payload = serialize_location(row, viewer_authorized=True, is_private_entity=False)
    assert payload is not None
    assert payload["precision"] == "approximate"
    assert payload["latitude"] == 51.51
    assert payload["longitude"] == -0.13


def test_serialize_exact_keeps_full_precision() -> None:
    row = {
        "id": uuid4(),
        "provider_place_id": "nominatim:2",
        "display_label": "Exact Venue",
        "latitude": Decimal("51.507351"),
        "longitude": Decimal("-0.127758"),
        "region": "London",
        "country": "United Kingdom",
        "precision": "exact",
        "is_online": False,
    }
    payload = serialize_location(row, viewer_authorized=True, is_private_entity=False)
    assert payload is not None
    assert payload["precision"] == "exact"
    assert payload["latitude"] == 51.507351
    assert payload["longitude"] == -0.127758


def test_serialize_redacts_private_location_for_unauthorized() -> None:
    row = {
        "id": uuid4(),
        "provider_place_id": None,
        "display_label": "Secret place",
        "latitude": Decimal("10.123456"),
        "longitude": Decimal("20.654321"),
        "region": None,
        "country": None,
        "precision": "exact",
        "is_online": False,
    }
    assert serialize_location(row, viewer_authorized=False, is_private_entity=True) is None
    authorized = serialize_location(row, viewer_authorized=True, is_private_entity=True)
    assert authorized is not None
    assert authorized["display_label"] == "Secret place"


def test_map_eligibility_rules() -> None:
    physical = {
        "latitude": Decimal("1.0"),
        "longitude": Decimal("2.0"),
        "is_online": False,
    }
    online = {
        "latitude": None,
        "longitude": None,
        "is_online": True,
    }

    assert is_map_eligible(entity_kind="event", location=physical) is True
    assert is_map_eligible(entity_kind="help_request", location=physical) is True
    assert is_map_eligible(entity_kind="event", location=online) is False

    assert is_map_eligible(entity_kind="event_activity", location=physical, scheduled=True) is True
    assert (
        is_map_eligible(entity_kind="event_activity", location=physical, scheduled=False) is False
    )
    assert (
        is_map_eligible(entity_kind="project_activity", location=physical, scheduled=True) is True
    )

    assert (
        is_map_eligible(
            entity_kind="project",
            location=physical,
            leading_plan_has_physical_location=False,
        )
        is False
    )
    assert (
        is_map_eligible(
            entity_kind="project",
            location=physical,
            leading_plan_has_physical_location=True,
        )
        is True
    )


def test_create_location_persists(db_session) -> None:
    row = create_location(
        db_session,
        {
            "display_label": "Central Park",
            "latitude": 40.7829,
            "longitude": -73.9654,
            "region": "New York",
            "country": "United States",
            "precision": "approximate",
            "provider_place_id": "nominatim:99",
        },
    )
    db_session.commit()
    assert row["display_label"] == "Central Park"
    assert row["precision"] == "approximate"
    assert float(row["latitude"]) == 40.7829


def test_search_places_caches_and_hides_credentials() -> None:
    settings = Settings(
        geocoding_provider_url="https://geocode.test",
        geocoding_provider_api_key="super-secret-key",
        geocoding_cache_ttl_seconds=120,
    )
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {
            "place_id": 42,
            "lat": "52.370216",
            "lon": "4.895168",
            "display_name": "Amsterdam, Netherlands",
            "address": {"city": "Amsterdam", "country": "Netherlands"},
        }
    ]

    redis = AsyncMock()
    redis.get.return_value = None
    redis.set = AsyncMock()

    async def run() -> list[dict]:
        with (
            patch("app.services.locations.geocoding.get_redis_client", return_value=redis),
            patch("httpx.AsyncClient") as client_cls,
        ):
            client = AsyncMock()
            client.get.return_value = mock_response
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            client_cls.return_value = client

            results = await search_places("Amsterdam", limit=3, settings=settings)
            called_url = client.get.await_args.args[0]
            assert "super-secret-key" in called_url
            return results

    results = asyncio.run(run())
    assert len(results) == 1
    assert results[0]["display_label"] == "Amsterdam, Netherlands"
    assert results[0]["provider_place_id"] == "nominatim:42"
    assert "key" not in results[0]
    assert "api_key" not in results[0]
    redis.set.assert_awaited()


def test_search_places_provider_failure() -> None:
    settings = Settings(geocoding_provider_url="https://geocode.test")
    redis = AsyncMock()
    redis.get.return_value = None

    async def run() -> None:
        with (
            patch("app.services.locations.geocoding.get_redis_client", return_value=redis),
            patch("httpx.AsyncClient") as client_cls,
        ):
            client = AsyncMock()
            client.get.side_effect = httpx.ConnectError("boom")
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            client_cls.return_value = client

            with pytest.raises(HTTPException) as exc:
                await search_places("Berlin", settings=settings)
            assert exc.value.status_code == 503
            assert exc.value.detail == "geocoding_provider_unavailable"

    asyncio.run(run())


def test_search_places_provider_rate_limit() -> None:
    settings = Settings(geocoding_provider_url="https://geocode.test")
    mock_response = MagicMock()
    mock_response.status_code = 429
    redis = AsyncMock()
    redis.get.return_value = None

    async def run() -> None:
        with (
            patch("app.services.locations.geocoding.get_redis_client", return_value=redis),
            patch("httpx.AsyncClient") as client_cls,
        ):
            client = AsyncMock()
            client.get.return_value = mock_response
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            client_cls.return_value = client

            with pytest.raises(HTTPException) as exc:
                await search_places("Berlin", settings=settings)
            assert exc.value.status_code == 429
            assert exc.value.detail == "geocoding_provider_rate_limited"

    asyncio.run(run())


def test_enforce_geocoding_rate_limit_blocks() -> None:
    settings = Settings(geocoding_rate_limit=1, geocoding_rate_limit_window_seconds=60)
    request = MagicMock()
    redis = AsyncMock()
    redis.incr.return_value = 2
    redis.expire = AsyncMock()

    async def run() -> None:
        with (
            patch("app.services.locations.geocoding.get_settings", return_value=settings),
            patch("app.services.locations.geocoding.get_redis_client", return_value=redis),
            patch("app.utils.request.get_client_ip", return_value="127.0.0.1"),
        ):
            with pytest.raises(HTTPException) as exc:
                await enforce_geocoding_rate_limit(request)
            assert exc.value.status_code == 429
            assert exc.value.detail == "geocoding_rate_limit_exceeded"

    asyncio.run(run())


def test_reverse_geocode_validates_input() -> None:
    async def run() -> None:
        with pytest.raises(HTTPException) as exc:
            await reverse_geocode(1000.0, 0.0)
        assert exc.value.detail == "location_coordinates_out_of_range"

    asyncio.run(run())


def test_validate_search_query() -> None:
    assert validate_search_query("  Parks  ") == "Parks"
    with pytest.raises(HTTPException):
        validate_search_query("   ")
