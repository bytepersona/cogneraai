"""Unit-Tests für utils.url_allowlist und utils.url_parse."""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.url_allowlist import domain_matches_allowlist, url_is_allowlisted
from utils.url_parse import extract_http_urls, hostname_from_url


# --- domain_matches_allowlist ---

def test_exact_match() -> None:
    assert domain_matches_allowlist("discord.com", ["discord.com"])


def test_subdomain_matches_parent() -> None:
    assert domain_matches_allowlist("cdn.discord.com", ["discord.com"])


def test_no_match() -> None:
    assert not domain_matches_allowlist("evil.com", ["discord.com", "cdn.discordapp.com"])


def test_empty_allowlist() -> None:
    assert not domain_matches_allowlist("discord.com", [])


def test_case_insensitive() -> None:
    assert domain_matches_allowlist("Discord.COM", ["discord.com"])


def test_deep_subdomain() -> None:
    assert domain_matches_allowlist("a.b.discord.com", ["discord.com"])


# --- url_is_allowlisted ---

def test_url_in_allowlist() -> None:
    assert url_is_allowlisted("https://discord.com/invite/abc", ["discord.com"])


def test_url_not_in_allowlist() -> None:
    assert not url_is_allowlisted("https://phishing.example.com/click", ["discord.com"])


def test_invalid_url_not_allowlisted() -> None:
    assert not url_is_allowlisted("not-a-url", ["discord.com"])


# --- extract_http_urls ---

def test_extract_single_url() -> None:
    urls = extract_http_urls("Check this out: https://discord.com/invite/test123")
    assert "https://discord.com/invite/test123" in urls


def test_extract_multiple_urls() -> None:
    text = "Visit https://example.com and also http://other.org/page"
    urls = extract_http_urls(text)
    assert len(urls) >= 2


def test_extract_no_urls() -> None:
    urls = extract_http_urls("No links here, just plain text.")
    assert urls == []


def test_no_duplicates() -> None:
    text = "https://discord.com https://discord.com"
    urls = extract_http_urls(text)
    assert len([u for u in urls if u == "https://discord.com"]) == 1


# --- hostname_from_url ---

def test_hostname_from_http() -> None:
    assert hostname_from_url("http://example.com/path") == "example.com"


def test_hostname_from_https() -> None:
    assert hostname_from_url("https://sub.example.com/") == "sub.example.com"


def test_hostname_from_invalid() -> None:
    assert hostname_from_url("not-a-url") is None


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
