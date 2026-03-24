"""Skill command helpers for AgentLoop."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

from nanobot.agent.i18n import text
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


class SkillCommandHandler:
    """Encapsulates `/skill` subcommand behavior for AgentLoop."""

    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop

    @staticmethod
    def _response(msg: InboundMessage, content: str) -> OutboundMessage:
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)

    @staticmethod
    def _decode_subprocess_output(data: bytes) -> str:
        """Decode subprocess output conservatively for CLI surfacing."""
        return data.decode("utf-8", errors="replace").strip()

    def _is_clawhub_network_error(self, output: str) -> bool:
        lowered = output.lower()
        return any(marker in lowered for marker in self.loop._CLAWHUB_NETWORK_ERROR_MARKERS)

    def _format_clawhub_error(self, language: str, code: int, output: str) -> str:
        if output and self._is_clawhub_network_error(output):
            return "\n\n".join([text(language, "skill_command_network_failed"), output])
        return output or text(language, "skill_command_failed", code=code)

    @staticmethod
    def _clawhub_search_headers(language: str) -> dict[str, str]:
        accept_language = "zh-CN,zh;q=0.9,en;q=0.8" if language.startswith("zh") else "en-US,en;q=0.9"
        return {
            "accept": "*/*",
            "accept-language": accept_language,
            "origin": "https://skillhub.tencent.com",
            "referer": "https://skillhub.tencent.com/",
        }

    def _format_clawhub_search_results(
        self,
        language: str,
        query: str,
        skills: list[dict[str, Any]],
        total: int,
    ) -> str:
        blocks = [
            text(
                language,
                "skill_search_results_header",
                query=query,
                total=total,
                count=len(skills),
            )
        ]
        description_key = "description_zh" if language.startswith("zh") else "description"
        for index, skill in enumerate(skills, start=1):
            name = str(skill.get("name") or skill.get("slug") or f"skill-{index}")
            slug = str(skill.get("slug") or "-")
            owner = str(skill.get("ownerName") or "-")
            installs = str(skill.get("installs") or 0)
            stars = str(skill.get("stars") or 0)
            version = str(skill.get("version") or "-")
            description = str(
                skill.get(description_key) or skill.get("description") or skill.get("description_zh") or ""
            ).strip()
            homepage = str(skill.get("homepage") or "").strip()
            lines = [
                f"{index}. {name}",
                text(
                    language,
                    "skill_search_result_meta",
                    slug=slug,
                    owner=owner,
                    installs=installs,
                    stars=stars,
                    version=version,
                ),
            ]
            if description:
                lines.append(description)
            if homepage:
                lines.append(homepage)
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    async def _search_clawhub(
        self,
        language: str,
        query: str,
    ) -> tuple[int, str]:
        params = {
            "page": "1",
            "pageSize": str(self.loop._CLAWHUB_SEARCH_LIMIT),
            "sortBy": "score",
            "order": "desc",
            "keyword": query,
        }
        try:
            async with httpx.AsyncClient(
                proxy=self.loop.web_proxy,
                follow_redirects=True,
                timeout=self.loop._CLAWHUB_SEARCH_TIMEOUT_SECONDS,
            ) as client:
                response = await client.get(
                    self.loop._CLAWHUB_SEARCH_API_URL,
                    params=params,
                    headers=self._clawhub_search_headers(language),
                )
                response.raise_for_status()
        except httpx.TimeoutException:
            return 124, text(language, "skill_search_timeout")
        except httpx.HTTPStatusError as exc:
            details = exc.response.text.strip()
            message = text(language, "skill_search_failed_status", status=exc.response.status_code)
            return exc.response.status_code, "\n\n".join(part for part in [message, details] if part)
        except httpx.RequestError as exc:
            return 1, "\n\n".join([text(language, "skill_search_request_failed"), str(exc)])

        try:
            payload = response.json()
        except ValueError:
            return 1, text(language, "skill_search_invalid_response")

        if not isinstance(payload, dict):
            return 1, text(language, "skill_search_invalid_response")

        if payload.get("code") != 0:
            details = str(payload.get("message") or "").strip()
            return 1, "\n\n".join(
                part for part in [text(language, "skill_search_failed"), details] if part
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            return 1, text(language, "skill_search_invalid_response")

        skills = data.get("skills")
        if not isinstance(skills, list):
            return 1, text(language, "skill_search_invalid_response")

        total = data.get("total")
        if not isinstance(total, int):
            total = len(skills)

        if not skills:
            return 0, ""

        return 0, self._format_clawhub_search_results(language, query, skills, total)

    def _clawhub_env(self) -> dict[str, str]:
        """Configure npm so ClawHub fails fast and uses a writable cache directory."""
        env = os.environ.copy()
        env.setdefault("NO_COLOR", "1")
        env.setdefault("FORCE_COLOR", "0")
        env.setdefault("npm_config_cache", str(ensure_dir(self.loop._clawhub_npm_cache_dir)))
        env.setdefault("npm_config_update_notifier", "false")
        env.setdefault("npm_config_audit", "false")
        env.setdefault("npm_config_fund", "false")
        env.setdefault("npm_config_fetch_retries", "0")
        env.setdefault("npm_config_fetch_timeout", "5000")
        env.setdefault("npm_config_fetch_retry_mintimeout", "1000")
        env.setdefault("npm_config_fetch_retry_maxtimeout", "5000")
        return env

    def _is_clawhub_cache_error(self, output: str) -> bool:
        lowered = output.lower()
        return any(marker in lowered for marker in self.loop._CLAWHUB_CACHE_ERROR_MARKERS) and (
            "_npx/" in lowered or "_npx\\" in lowered
        )

    @staticmethod
    def _clear_clawhub_exec_cache(env: dict[str, str]) -> None:
        """Clear npm's temporary exec installs without wiping the shared tarball cache."""
        cache_root = env.get("npm_config_cache")
        if not cache_root:
            return
        shutil.rmtree(Path(cache_root) / "_npx", ignore_errors=True)

    async def _run_clawhub_once(
        self,
        npx: str,
        env: dict[str, str],
        *args: str,
        timeout_seconds: int | None = None,
    ) -> tuple[int, str]:
        """Run one ClawHub subprocess attempt and return (exit_code, combined_output)."""
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                npx,
                "--yes",
                "clawhub@latest",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds or self.loop._CLAWHUB_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            raise
        except asyncio.TimeoutError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.communicate()
            raise
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.communicate()
            raise

        output_parts = [
            self._decode_subprocess_output(stdout),
            self._decode_subprocess_output(stderr),
        ]
        output = "\n".join(part for part in output_parts if part).strip()
        return proc.returncode or 0, output

    @staticmethod
    def _clawhub_args(workspace: str, *args: str) -> tuple[str, ...]:
        """Build ClawHub CLI args with global options first for consistent parsing."""
        return ("--workdir", workspace, "--no-input", *args)

    @staticmethod
    def _is_valid_skill_slug(slug: str) -> bool:
        """Validate a workspace skill slug for local install/remove operations."""
        return bool(slug) and slug not in {".", ".."} and "/" not in slug and "\\" not in slug

    def _prune_clawhub_lockfile(self, slug: str) -> bool:
        """Best-effort removal of a skill entry from the local ClawHub lockfile."""
        lock_path = self.loop.workspace / ".clawhub" / "lock.json"
        if not lock_path.exists():
            return False

        data = json.loads(lock_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False

        changed = False
        skills = data.get("skills")
        if isinstance(skills, dict) and slug in skills:
            del skills[slug]
            changed = True
        elif isinstance(skills, list):
            filtered = [
                item
                for item in skills
                if not (
                    item == slug
                    or (isinstance(item, dict) and (item.get("slug") == slug or item.get("name") == slug))
                )
            ]
            if len(filtered) != len(skills):
                data["skills"] = filtered
                changed = True

        if changed:
            lock_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return changed

    async def _run_clawhub(
        self, language: str, *args: str, timeout_seconds: int | None = None,
    ) -> tuple[int, str]:
        """Run the ClawHub CLI and return (exit_code, combined_output)."""
        npx = shutil.which("npx")
        if not npx:
            return 127, text(language, "skill_npx_missing")

        env = self._clawhub_env()

        try:
            async with self.loop._clawhub_lock:
                code, output = await self._run_clawhub_once(
                    npx,
                    env,
                    *args,
                    timeout_seconds=timeout_seconds,
                )
                if code != 0 and self._is_clawhub_cache_error(output):
                    logger.warning(
                        "Retrying ClawHub command after clearing npm exec cache at {}",
                        env["npm_config_cache"],
                    )
                    self._clear_clawhub_exec_cache(env)
                    code, output = await self._run_clawhub_once(
                        npx,
                        env,
                        *args,
                        timeout_seconds=timeout_seconds,
                    )
        except FileNotFoundError:
            return 127, text(language, "skill_npx_missing")
        except asyncio.TimeoutError:
            return 124, text(language, "skill_command_timeout")
        except asyncio.CancelledError:
            raise
        return code, output

    def _format_skill_command_success(
        self,
        language: str,
        subcommand: str,
        output: str,
        *,
        include_workspace_note: bool = False,
    ) -> str:
        notes: list[str] = []
        if output:
            notes.append(output)
        if include_workspace_note:
            notes.append(text(language, "skill_applied_to_workspace", workspace=self.loop.workspace))
        return "\n\n".join(notes) if notes else text(language, "skill_command_completed", command=subcommand)

    async def _run_skill_clawhub_command(
        self,
        msg: InboundMessage,
        language: str,
        subcommand: str,
        *args: str,
        timeout_seconds: int | None = None,
        include_workspace_note: bool = False,
    ) -> OutboundMessage:
        code, output = await self._run_clawhub(
            language,
            *self._clawhub_args(str(self.loop.workspace), *args),
            timeout_seconds=timeout_seconds,
        )
        if code != 0:
            return self._response(msg, self._format_clawhub_error(language, code, output))
        return self._response(
            msg,
            self._format_skill_command_success(
                language,
                subcommand,
                output,
                include_workspace_note=include_workspace_note,
            ),
        )

    async def search(self, msg: InboundMessage, language: str, query: str) -> OutboundMessage:
        code, output = await self._search_clawhub(language, query)
        if code != 0:
            return self._response(msg, output or text(language, "skill_search_failed"))
        if not output:
            return self._response(msg, text(language, "skill_search_no_results", query=query))
        return self._response(msg, output)

    async def install(self, msg: InboundMessage, language: str, slug: str) -> OutboundMessage:
        return await self._run_skill_clawhub_command(
            msg,
            language,
            "install",
            "install",
            slug,
            timeout_seconds=self.loop._CLAWHUB_INSTALL_TIMEOUT_SECONDS,
            include_workspace_note=True,
        )

    async def uninstall(self, msg: InboundMessage, language: str, slug: str) -> OutboundMessage:
        if not self._is_valid_skill_slug(slug):
            return self._response(msg, text(language, "skill_invalid_slug", slug=slug))

        skill_dir = self.loop.workspace / "skills" / slug
        if not skill_dir.is_dir():
            return self._response(
                msg,
                text(language, "skill_uninstall_not_found", slug=slug, path=skill_dir),
            )

        try:
            shutil.rmtree(skill_dir)
        except OSError:
            logger.exception("Failed to remove workspace skill {}", skill_dir)
            return self._response(
                msg,
                text(language, "skill_uninstall_failed", slug=slug, path=skill_dir),
            )

        notes = [text(language, "skill_uninstalled_local", slug=slug, path=skill_dir)]
        try:
            if self._prune_clawhub_lockfile(slug):
                notes.append(
                    text(
                        language,
                        "skill_lockfile_pruned",
                        path=self.loop.workspace / ".clawhub" / "lock.json",
                    )
                )
        except (OSError, ValueError, TypeError):
            logger.exception("Failed to prune ClawHub lockfile for {}", slug)
            notes.append(
                text(
                    language,
                    "skill_lockfile_cleanup_failed",
                    path=self.loop.workspace / ".clawhub" / "lock.json",
                )
            )

        return self._response(msg, "\n\n".join(notes))

    async def list(self, msg: InboundMessage, language: str) -> OutboundMessage:
        return await self._run_skill_clawhub_command(msg, language, "list", "list")

    async def update(self, msg: InboundMessage, language: str) -> OutboundMessage:
        return await self._run_skill_clawhub_command(
            msg,
            language,
            "update",
            "update",
            "--all",
            timeout_seconds=self.loop._CLAWHUB_INSTALL_TIMEOUT_SECONDS,
            include_workspace_note=True,
        )
