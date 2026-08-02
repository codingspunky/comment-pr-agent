"""Thin GitHub REST client scoped to one token."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import get_settings


@dataclass
class PullRequest:
    number: int
    head_ref: str
    base_ref: str
    head_repo_full_name: str
    state: str
    html_url: str


class GitHubAPI:
    def __init__(self, token: str, api_base: Optional[str] = None):
        self.token = token
        self.api_base = api_base or get_settings().github_api_base
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        # GitHub returns 5xx and secondary-rate-limit 403/429 under load; a
        # single un-retried failure used to abort a run that had already done
        # all its expensive work.
        retry = Retry(total=3, backoff_factor=1.0,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset({"GET", "POST"}),
                      raise_on_status=False)
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _url(self, path: str) -> str:
        return f"{self.api_base}{path}"

    def get_default_branch(self, repo: str) -> str:
        r = self.session.get(self._url(f"/repos/{repo}"), timeout=30)
        r.raise_for_status()
        return r.json()["default_branch"]

    def get_pull_request(self, repo: str, number: int) -> PullRequest:
        r = self.session.get(self._url(f"/repos/{repo}/pulls/{number}"), timeout=30)
        r.raise_for_status()
        d = r.json()
        return PullRequest(
            number=d["number"],
            head_ref=d["head"]["ref"],
            base_ref=d["base"]["ref"],
            head_repo_full_name=d["head"]["repo"]["full_name"] if d["head"].get("repo") else repo,
            state=d["state"],
            html_url=d["html_url"],
        )

    def get_pull_request_diff(self, repo: str, number: int) -> str:
        r = self.session.get(
            self._url(f"/repos/{repo}/pulls/{number}"),
            headers={"Accept": "application/vnd.github.v3.diff"},
            timeout=30,
        )
        r.raise_for_status()
        return r.text

    def create_pull_request(
        self, repo: str, title: str, body: str, head: str, base: str) -> PullRequest:
        r = self.session.post(
            self._url(f"/repos/{repo}/pulls"),
            json={"title": title, "body": body, "head": head, "base": base},
            timeout=30,
        )
        r.raise_for_status()
        d = r.json()
        return PullRequest(
            number=d["number"],
            head_ref=d["head"]["ref"],
            base_ref=d["base"]["ref"],
            head_repo_full_name=repo,
            state=d["state"],
            html_url=d["html_url"],
        )

    def request_reviewers(self, repo: str, number: int, reviewers: List[str]) -> None:
        if not reviewers:
            return
        r = self.session.post(
            self._url(f"/repos/{repo}/pulls/{number}/requested_reviewers"),
            json={"reviewers": reviewers},
            timeout=30,
        )
        # 422 happens if a reviewer is the PR author or already requested -- non-fatal.
        if r.status_code not in (200, 201, 422):
            r.raise_for_status()

    def create_comment(self, repo: str, issue_number: int, body: str) -> None:
        """Post a comment on an issue or PR conversation (same endpoint)."""
        r = self.session.post(
            self._url(f"/repos/{repo}/issues/{issue_number}/comments"),
            json={"body": body},
            timeout=30,
        )
        r.raise_for_status()
