"""
Echtzeit-Moderation: Warteschlange, Claude, Strikes, Review-Queue, Dry-Run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import timedelta, timezone
from typing import Any, Optional

import discord
from cachetools import TTLCache
from discord.ext import commands

from core_bot import ModerationBot
from utils.circuit_breaker import CircuitOpenError
from utils.discord_embeds import build_mod_log_embed, build_user_notice_embed
from utils.models import ClaudeModerationResponse, ModerationDecision, Severity
from utils.moderation_jobs import ModerationJob
from utils.prompts import MODERATOR_AI_SYSTEM_PROMPT, build_user_payload
from utils.strike_escalation import cap_decision_by_strikes
from utils.url_allowlist import url_is_allowlisted
from utils.url_parse import extract_http_urls
from utils.virustotal_client import UrlScanVerdict

logger = logging.getLogger(__name__)


def _moderation_eval_json(d: ClaudeModerationResponse) -> str:
    """Serielle Auswertung für DB und „See Evaluation“."""
    return json.dumps(d.model_dump(mode="json"), ensure_ascii=False)


def _message_snapshot(message: discord.Message) -> str:
    return (message.content or "")[:4000]


class ModerationCog(commands.Cog):
    """Lauscht `on_message` und steuert die KI-Moderation (Worker-Queue)."""

    def __init__(self, bot: ModerationBot) -> None:
        self.bot = bot
        self._worker_task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        self._worker_task = asyncio.create_task(self._moderation_worker())

    async def cog_unload(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    async def _moderation_worker(self) -> None:
        q = self.bot.moderation_queue
        if q is None:
            return
        while True:
            job = await q.get()
            try:
                await self._process_job(job)
            except Exception:
                logger.exception("Worker: Fehler bei Job %s", job)
            finally:
                q.task_done()

    async def _process_job(self, job: ModerationJob) -> None:
        guild = self.bot.get_guild(job.guild_id)
        if guild is None:
            return
        ch = guild.get_channel(job.channel_id)
        if not isinstance(ch, discord.TextChannel):
            return
        try:
            message = await ch.fetch_message(job.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logger.debug("Message nicht abrufbar: %s", e)
            return
        await self._run_ai_pipeline(message, job.event_ref)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return

        db = self.bot.db
        settings = self.bot.settings
        rate = self.bot.rate_limiter
        cache: TTLCache[str, bool] = self.bot.msg_cache
        queue = self.bot.moderation_queue

        if db is None or rate is None or cache is None or queue is None:
            logger.error("Bot nicht vollständig initialisiert — Nachricht ignoriert.")
            return

        event_ref = await self._persist_only(db, message)

        gcfg = await db.get_guild_config(message.guild.id)
        need_ai = bool(gcfg["ai_enabled"])
        need_url_scan = bool(gcfg.get("url_scan_enabled")) and self.bot.vt_client is not None
        if not need_ai and not need_url_scan:
            return

        if self._is_whitelisted(message, gcfg):
            return

        if not rate.allow(message.author.id):
            logger.warning("Rate-Limit für User %s — keine API.", message.author.id)
            return

        content_display = (message.content or "").strip()
        urls_in_msg = extract_http_urls(message.content or "")
        if not content_display and not message.attachments and not urls_in_msg:
            return

        if need_ai:
            cache_key = self._cache_key(message)
            if cache_key in cache:
                logger.debug("Cache-Treffer — keine erneute API für Hash %s...", cache_key[:12])
                return
        elif not urls_in_msg:
            return

        job = ModerationJob(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            message_id=message.id,
            event_ref=event_ref,
        )
        try:
            queue.put_nowait(job)
        except asyncio.QueueFull:
            logger.error("Moderationsqueue voll — Nachricht %s nicht analysiert.", message.id)
            return
        if need_ai:
            cache[cache_key] = True

    async def _run_ai_pipeline(
        self,
        message: discord.Message,
        event_ref: Optional[str] = None,
    ) -> None:
        db = self.bot.db
        settings = self.bot.settings
        ai = self.bot.ai
        breaker = self.bot.anthropic_breaker
        if db is None or ai is None or breaker is None:
            return

        gcfg = await db.get_guild_config(message.guild.id)
        if self._is_whitelisted(message, gcfg):
            return

        url_decision = await self._evaluate_urls_vt(message, gcfg)
        if url_decision is not None:
            case_ref = await db.allocate_case_ref(message.guild.id)
            new_strikes = await db.increment_user_strike(message.guild.id, message.author.id)
            if gcfg.get("strike_escalation_enabled"):
                effective = cap_decision_by_strikes(
                    url_decision,
                    new_strikes,
                    gcfg.get("strike_escalation_json") or "",
                )
            else:
                effective = url_decision
            if self._needs_review(url_decision, effective, gcfg):
                payload = json.dumps(effective.model_dump(mode="json"), ensure_ascii=False)
                qid = await db.insert_review_queue(
                    message.guild.id,
                    message.channel.id,
                    message.id,
                    message.author.id,
                    effective.moderation_decision.value,
                    payload,
                    jump_url=message.jump_url,
                    case_ref=case_ref,
                )
                await self._post_review_embed(
                    message, url_decision, effective, gcfg, qid, case_ref=case_ref, event_ref=event_ref
                )
                await db.add_mod_log(
                    message.guild.id,
                    message.author.id,
                    "review_queued",
                    effective.reason,
                    channel_id=message.channel.id,
                    actor_id=None,
                    details=f"queue_id={qid} | strikes={new_strikes} | url_scan=vt",
                    case_ref=case_ref,
                    evaluation_json=_moderation_eval_json(effective),
                    message_content_snapshot=_message_snapshot(message),
                )
                return
            await self._execute_decision(
                message, effective, gcfg, db, case_ref=case_ref, event_ref=event_ref
            )
            return

        if not gcfg["ai_enabled"]:
            return

        ctx_limit = settings.context_message_count
        recent = await db.fetch_recent_messages(message.channel.id, ctx_limit)
        warnings_block = await db.fetch_recent_warnings_text(message.guild.id, message.author.id)
        thr = int(gcfg["confidence_threshold"])

        system_prompt = MODERATOR_AI_SYSTEM_PROMPT.format(
            server_rules=gcfg["server_rules"],
            context_block=self._format_context(recent, warnings_block, message),
        )
        user_payload = build_user_payload(self._format_new_message(message))

        try:
            async def _call() -> ClaudeModerationResponse:
                return await ai.moderate(
                    system_prompt=system_prompt,
                    user_payload=user_payload,
                    guild_confidence_threshold=thr,
                )

            decision = await breaker.call(_call)
        except CircuitOpenError:
            logger.warning("Anthropic-Circuit offen — Moderation übersprungen (msg %s).", message.id)
            return
        except Exception:
            logger.exception("Moderations-API fehlgeschlagen für Message %s", message.id)
            return

        if decision.moderation_decision == ModerationDecision.ALLOW:
            return

        case_ref = await db.allocate_case_ref(message.guild.id)
        new_strikes = await db.increment_user_strike(message.guild.id, message.author.id)
        if gcfg.get("strike_escalation_enabled"):
            effective = cap_decision_by_strikes(
                decision,
                new_strikes,
                gcfg.get("strike_escalation_json") or "",
            )
        else:
            effective = decision

        if self._needs_review(decision, effective, gcfg):
            payload = json.dumps(effective.model_dump(mode="json"), ensure_ascii=False)
            qid = await db.insert_review_queue(
                message.guild.id,
                message.channel.id,
                message.id,
                message.author.id,
                effective.moderation_decision.value,
                payload,
                jump_url=message.jump_url,
                case_ref=case_ref,
            )
            await self._post_review_embed(
                message, decision, effective, gcfg, qid, case_ref=case_ref, event_ref=event_ref
            )
            await db.add_mod_log(
                message.guild.id,
                message.author.id,
                "review_queued",
                effective.reason,
                channel_id=message.channel.id,
                actor_id=None,
                details=f"queue_id={qid} | strikes={new_strikes}",
                case_ref=case_ref,
                evaluation_json=_moderation_eval_json(effective),
                message_content_snapshot=_message_snapshot(message),
            )
            return

        await self._execute_decision(message, effective, gcfg, db, case_ref=case_ref, event_ref=event_ref)

    @staticmethod
    def _needs_review(
        orig: ClaudeModerationResponse,
        effective: ClaudeModerationResponse,
        gcfg: dict[str, Any],
    ) -> bool:
        if not gcfg.get("review_queue_enabled", True):
            return False
        floor = int(gcfg.get("review_confidence_floor", 50))
        if orig.requires_manual_review:
            return True
        if orig.confidence < floor:
            return True
        if effective.moderation_decision == ModerationDecision.BAN:
            return True
        if orig.moderation_decision == ModerationDecision.BAN:
            return True
        return False

    async def _post_review_embed(
        self,
        message: discord.Message,
        orig: ClaudeModerationResponse,
        effective: ClaudeModerationResponse,
        gcfg: dict[str, Any],
        queue_id: int,
        *,
        case_ref: Optional[str] = None,
        event_ref: Optional[str] = None,
    ) -> None:
        cid = gcfg.get("mod_log_channel_id")
        if not cid:
            logger.warning("Review-Queue ohne mod_log_channel — Queue-ID %s.", queue_id)
            return
        guild = message.guild
        ch = guild.get_channel(int(cid))
        if not isinstance(ch, discord.TextChannel):
            return

        title = f"Moderation: Manuelle Freigabe{f' · `{case_ref}`' if case_ref else ''}"
        embed = discord.Embed(
            title=title,
            description=f"[Zur Nachricht]({message.jump_url})",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Nutzer", value=f"{message.author} ({message.author.id})", inline=False)
        embed.add_field(name="Vorschlag (nach Strikes)", value=effective.moderation_decision.value, inline=True)
        embed.add_field(name="Confidence", value=str(orig.confidence), inline=True)
        embed.add_field(name="Grund", value=(effective.reason or "")[:900], inline=False)
        if case_ref:
            embed.add_field(name="Fall-ID", value=f"`{case_ref}`", inline=True)
        if event_ref:
            embed.add_field(name="Ereignis-ID", value=f"`{event_ref}`", inline=True)
        foot = f"ModeratorAI · Review · queue_id={queue_id}"
        embed.set_footer(text=foot)
        embed.timestamp = discord.utils.utcnow()

        view = ReviewView(self.bot, queue_id)
        delete_after = self._resolve_embed_delete_after(gcfg)
        try:
            await ch.send(embed=embed, view=view, delete_after=delete_after)
        except discord.HTTPException:
            logger.exception("Konnte Review-Embed nicht senden.")

    async def execute_decision_for_review(
        self,
        message: discord.Message,
        effective: ClaudeModerationResponse,
        *,
        case_ref: Optional[str] = None,
        event_ref: Optional[str] = None,
    ) -> None:
        """Manuelle Review-Freigabe: setzt um (Dry-Run wird umgangen)."""
        db = self.bot.db
        if db is None:
            return
        gcfg = dict(await db.get_guild_config(message.guild.id))
        gcfg["dry_run"] = False
        await self._execute_decision(
            message, effective, gcfg, db, case_ref=case_ref, event_ref=event_ref
        )

    async def _persist_only(self, db: Any, message: discord.Message) -> str:
        ts = message.created_at.astimezone(timezone.utc).isoformat()
        return await db.insert_message(
            message.guild.id,
            message.channel.id,
            message.id,
            message.author.id,
            str(message.author),
            message.content or "",
            ts,
        )

    def _resolve_embed_delete_after(self, gcfg: dict[str, Any]) -> Optional[float]:
        raw = gcfg.get("mod_embed_delete_after_seconds")
        if raw is not None:
            if int(raw) <= 0:
                return None
            return float(raw)
        bs = self.bot.settings.bot_message_delete_after_seconds
        return float(bs) if bs > 0 else None

    async def _get_vt_verdict_cached(self, url: str) -> Optional[UrlScanVerdict]:
        cache = self.bot.vt_url_cache
        vt = self.bot.vt_client
        if vt is None:
            return None
        if cache is not None and url in cache:
            return cache[url]  # type: ignore[return-value]
        verdict = await vt.get_url_verdict(url)
        if cache is not None and verdict is not None:
            cache[url] = verdict
        return verdict

    async def _evaluate_urls_vt(
        self,
        message: discord.Message,
        gcfg: dict[str, Any],
    ) -> Optional[ClaudeModerationResponse]:
        if not gcfg.get("url_scan_enabled"):
            return None
        if self.bot.vt_client is None:
            return None
        urls = extract_http_urls(message.content or "")
        if not urls:
            return None
        patterns = list(gcfg.get("url_allowlist_domains") or [])
        mal_thr = int(gcfg.get("vt_malicious_threshold", 1))
        sus_thr = int(gcfg.get("vt_suspicious_threshold", 3))
        for url in urls:
            if url_is_allowlisted(url, patterns):
                continue
            verdict = await self._get_vt_verdict_cached(url)
            if verdict is None:
                continue
            if verdict.malicious >= mal_thr or verdict.suspicious >= sus_thr:
                return ClaudeModerationResponse(
                    moderation_decision=ModerationDecision.DELETE,
                    confidence=100,
                    severity=Severity.HIGH,
                    reason="Link durch VirusTotal als riskant eingestuft",
                    explanation=(
                        f"URL: {url[:900]}\n"
                        f"VirusTotal: malicious={verdict.malicious}, "
                        f"suspicious={verdict.suspicious}, harmless={verdict.harmless}"
                    ),
                    user_facing_message=(
                        "Ein Link in deiner Nachricht wurde automatisch als riskant eingestuft."
                    ),
                    requires_manual_review=False,
                )
        return None

    def _is_whitelisted(self, message: discord.Message, gcfg: dict) -> bool:
        uid = message.author.id
        ch = message.channel.id
        if uid in gcfg.get("whitelist_user_ids", []):
            return True
        if ch in gcfg.get("whitelist_channel_ids", []):
            return True
        roles = getattr(message.author, "roles", []) or []
        allowed_roles = set(gcfg.get("whitelist_role_ids", []))
        for r in roles:
            if r.id in allowed_roles:
                return True
        return False

    @staticmethod
    def _cache_key(message: discord.Message) -> str:
        raw = f"{message.guild.id}:{message.channel.id}:{message.author.id}:{message.content or ''}"
        return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()

    @staticmethod
    def _format_new_message(message: discord.Message) -> str:
        ts = message.created_at.astimezone(timezone.utc).isoformat()
        attach = ""
        if message.attachments:
            names = ", ".join(a.filename for a in message.attachments[:5])
            attach = f"\nAnhänge: {names}"
        body = message.content or "(leer / nur Anhang)"
        return (
            f"Zeit: {ts}\n"
            f"Autor: {message.author} (ID {message.author.id})\n"
            f"Kanal: #{message.channel} (ID {message.channel.id})\n"
            f"Nachrichten-ID: {message.id}\n"
            f"Inhalt:\n{body}{attach}"
        )

    @staticmethod
    def _format_context(
        recent: list,
        warnings_block: str,
        message: discord.Message,
    ) -> str:
        lines: list[str] = []
        lines.append("#### Verwarnungen (Nutzer)")
        lines.append(warnings_block)
        lines.append("")
        lines.append("#### Letzte Channel-Nachrichten (chronologisch, inkl. aktueller Eintrag)")
        for m in recent:
            mark = " **← zu prüfend**" if m.message_id == message.id else ""
            ev = f" `{m.event_ref}`" if getattr(m, "event_ref", None) else ""
            lines.append(
                f"- [{m.created_at_iso}]{ev} {m.author_name} (ID {m.author_id}): "
                f"{m.content[:500]}{mark}",
            )
        return "\n".join(lines)

    async def _execute_decision(
        self,
        message: discord.Message,
        d: ClaudeModerationResponse,
        gcfg: dict,
        db,
        *,
        case_ref: Optional[str] = None,
        event_ref: Optional[str] = None,
    ) -> None:
        """Wendet delete / timeout / ban an und protokolliert."""
        log_details = d.explanation or d.reason
        jump_url = message.jump_url

        if gcfg.get("dry_run"):
            _, cr = await db.add_mod_log(
                message.guild.id,
                message.author.id,
                f"dry_run_{d.moderation_decision.value}",
                d.reason,
                channel_id=message.channel.id,
                actor_id=None,
                details=f"[DRY-RUN] {log_details}",
                case_ref=case_ref,
                evaluation_json=_moderation_eval_json(d),
                message_content_snapshot=_message_snapshot(message),
            )
            await self._maybe_post_mod_log(
                message.guild,
                gcfg,
                decision=d.moderation_decision,
                target_user=message.author,
                reason=d.reason,
                detail=log_details,
                jump_url=jump_url,
                simulated=True,
                timeout_minutes=d.timeout_minutes,
                case_ref=cr,
                event_ref=event_ref,
            )
            return

        guild = message.guild
        member = message.author
        if not isinstance(member, discord.Member):
            member = guild.get_member(message.author.id) or message.author

        try:
            if d.moderation_decision == ModerationDecision.WARN:
                await self._send_user_notice(
                    message,
                    d.user_facing_message or d.reason,
                    decision=d.moderation_decision,
                )
                await db.add_warning(
                    guild.id,
                    message.author.id,
                    d.reason or "KI-Verwarnung",
                    moderator_id=None,
                    source="ai",
                )
                _, cr = await db.add_mod_log(
                    guild.id,
                    message.author.id,
                    "warn",
                    d.reason,
                    channel_id=message.channel.id,
                    actor_id=None,
                    details=log_details,
                    case_ref=case_ref,
                    evaluation_json=_moderation_eval_json(d),
                    message_content_snapshot=_message_snapshot(message),
                )
                await self._maybe_post_mod_log(
                    guild,
                    gcfg,
                    decision=d.moderation_decision,
                    target_user=message.author,
                    reason=d.reason,
                    detail=log_details,
                    jump_url=jump_url,
                    case_ref=cr,
                    event_ref=event_ref,
                )

            elif d.moderation_decision == ModerationDecision.DELETE:
                await self._safe_delete(message)
                await self._send_user_notice(
                    message,
                    d.user_facing_message or "Deine Nachricht wurde entfernt.",
                    decision=d.moderation_decision,
                )
                _, cr = await db.add_mod_log(
                    guild.id,
                    message.author.id,
                    "delete",
                    d.reason,
                    channel_id=message.channel.id,
                    actor_id=None,
                    details=log_details,
                    case_ref=case_ref,
                    evaluation_json=_moderation_eval_json(d),
                    message_content_snapshot=_message_snapshot(message),
                )
                await self._maybe_post_mod_log(
                    guild,
                    gcfg,
                    decision=d.moderation_decision,
                    target_user=message.author,
                    reason=d.reason,
                    detail=log_details,
                    jump_url=jump_url,
                    case_ref=cr,
                    event_ref=event_ref,
                )

            elif d.moderation_decision == ModerationDecision.TIMEOUT:
                minutes = d.timeout_minutes or int(gcfg["default_timeout_minutes"])
                minutes = max(1, min(minutes, 40320))
                if isinstance(member, discord.Member):
                    await member.timeout(
                        timedelta(minutes=minutes),
                        reason=f"KI-Timeout: {d.reason}",
                    )
                await self._send_user_notice(
                    message,
                    d.user_facing_message or f"Du wurdest für {minutes} Minuten stummgeschaltet.",
                    decision=d.moderation_decision,
                )
                _, cr = await db.add_mod_log(
                    guild.id,
                    message.author.id,
                    "timeout",
                    d.reason,
                    channel_id=message.channel.id,
                    actor_id=None,
                    details=f"{log_details} | Minuten: {minutes}",
                    case_ref=case_ref,
                    evaluation_json=_moderation_eval_json(d),
                    message_content_snapshot=_message_snapshot(message),
                )
                await self._maybe_post_mod_log(
                    guild,
                    gcfg,
                    decision=d.moderation_decision,
                    target_user=message.author,
                    reason=d.reason,
                    detail=log_details,
                    jump_url=jump_url,
                    timeout_minutes=minutes,
                    case_ref=cr,
                    event_ref=event_ref,
                )

            elif d.moderation_decision == ModerationDecision.BAN:
                if isinstance(member, discord.Member):
                    await guild.ban(
                        member,
                        reason=f"KI-Ban: {d.reason}",
                        delete_message_days=1,
                    )
                else:
                    await guild.ban(
                        discord.Object(id=message.author.id),
                        reason=f"KI-Ban: {d.reason}",
                    )
                _, cr = await db.add_mod_log(
                    guild.id,
                    message.author.id,
                    "ban",
                    d.reason,
                    channel_id=message.channel.id,
                    actor_id=None,
                    details=log_details,
                    case_ref=case_ref,
                    evaluation_json=_moderation_eval_json(d),
                    message_content_snapshot=_message_snapshot(message),
                )
                await self._maybe_post_mod_log(
                    guild,
                    gcfg,
                    decision=d.moderation_decision,
                    target_user=message.author,
                    reason=d.reason,
                    detail=log_details,
                    jump_url=jump_url,
                    case_ref=cr,
                    event_ref=event_ref,
                )

        except discord.Forbidden:
            logger.error("Fehlende Bot-Rechte für Aktion %s", d.moderation_decision)
        except discord.HTTPException as e:
            logger.error("Discord HTTP-Fehler bei Moderation: %s", e)

    async def _safe_delete(self, message: discord.Message) -> None:
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

    async def _send_user_notice(
        self,
        message: discord.Message,
        text: str,
        *,
        decision: ModerationDecision,
    ) -> None:
        if not text:
            return
        embed = build_user_notice_embed(text, decision)
        member = message.author
        try:
            if isinstance(member, discord.Member):
                await member.send(embed=embed)
            else:
                await message.author.send(embed=embed)
        except discord.Forbidden:
            try:
                await message.channel.send(
                    content=message.author.mention,
                    embed=embed,
                    delete_after=120,
                )
            except discord.HTTPException:
                logger.warning("Konnte weder DM noch Channel-Hinweis senden.")

    async def _maybe_post_mod_log(
        self,
        guild: discord.Guild,
        gcfg: dict,
        *,
        decision: ModerationDecision,
        target_user: discord.User,
        reason: str,
        detail: Optional[str] = None,
        jump_url: Optional[str] = None,
        simulated: bool = False,
        timeout_minutes: Optional[int] = None,
        case_ref: Optional[str] = None,
        event_ref: Optional[str] = None,
    ) -> None:
        cid = gcfg.get("mod_log_channel_id")
        if not cid:
            return
        ch = guild.get_channel(int(cid))
        if not isinstance(ch, discord.TextChannel):
            return
        embed = build_mod_log_embed(
            decision=decision,
            target_display=str(target_user),
            target_id=target_user.id,
            reason=reason,
            jump_url=jump_url,
            detail=detail,
            simulated=simulated,
            timeout_minutes=timeout_minutes,
            case_ref=case_ref,
            event_ref=event_ref,
        )
        delete_after = self._resolve_embed_delete_after(gcfg)
        try:
            await ch.send(embed=embed, delete_after=delete_after)
        except discord.HTTPException:
            logger.warning("Mod-Log-Channel nicht beschreibbar.")


class ReviewView(discord.ui.View):
    """Buttons zur Freigabe gespeicherter Review-Einträge."""

    def __init__(self, bot: ModerationBot, queue_id: int) -> None:
        super().__init__(timeout=86400.0)
        self.bot = bot
        self.queue_id = queue_id

    @staticmethod
    def _can_mod(member: discord.Member) -> bool:
        return bool(member.guild_permissions.manage_guild or member.guild_permissions.ban_members)

    @discord.ui.button(label="Bestätigen", style=discord.ButtonStyle.danger, row=0)
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run(interaction, "approve")

    @discord.ui.button(label="Ablehnen", style=discord.ButtonStyle.secondary, row=0)
    async def dismiss_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run(interaction, "dismiss")

    @discord.ui.button(label="Nur Timeout 10m", style=discord.ButtonStyle.primary, row=1)
    async def timeout_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run(interaction, "timeout_soft")

    async def _run(self, interaction: discord.Interaction, action: str) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Ungültig.", ephemeral=True)
            return
        if not self._can_mod(interaction.user):
            await interaction.response.send_message("Fehlende Berechtigung.", ephemeral=True)
            return

        db = self.bot.db
        if db is None:
            await interaction.response.send_message("Datenbank nicht bereit.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        entry = await db.get_review_queue_entry(self.queue_id)
        if entry is None or entry.status != "pending":
            await interaction.followup.send("Eintrag nicht mehr aktiv.", ephemeral=True)
            return

        guild = self.bot.get_guild(entry.guild_id)
        if guild is None:
            await interaction.followup.send("Server nicht gefunden.", ephemeral=True)
            return

        ch = guild.get_channel(entry.channel_id)
        if not isinstance(ch, discord.TextChannel):
            await interaction.followup.send("Kanal nicht gefunden.", ephemeral=True)
            return

        try:
            message = await ch.fetch_message(entry.message_id)
        except (discord.NotFound, discord.Forbidden):
            await interaction.followup.send("Nachricht nicht mehr verfügbar.", ephemeral=True)
            await db.update_review_queue_status(self.queue_id, "resolved_missing")
            return

        mod_cog = self.bot.get_cog("ModerationCog")
        if mod_cog is None or not isinstance(mod_cog, ModerationCog):
            await interaction.followup.send("Moderations-Cog fehlt.", ephemeral=True)
            return

        if action == "dismiss":
            await db.update_review_queue_status(self.queue_id, "resolved_dismissed")
            await interaction.followup.send("Meldung abgelehnt (keine Aktion).", ephemeral=True)
            return

        try:
            data = json.loads(entry.payload_json)
            base = ClaudeModerationResponse.model_validate_loose(data)
        except Exception:
            logger.exception("Review-Payload ungültig queue=%s", self.queue_id)
            await interaction.followup.send("Gespeicherte Entscheidung ungültig.", ephemeral=True)
            return

        if action == "timeout_soft":
            eff = base.model_copy(
                update={
                    "moderation_decision": ModerationDecision.TIMEOUT,
                    "timeout_minutes": 10,
                    "explanation": (base.explanation or "") + " [Review: Nur Timeout 10m]",
                },
            )
        else:
            eff = base

        await mod_cog.execute_decision_for_review(
            message, eff, case_ref=entry.case_ref
        )
        await db.update_review_queue_status(self.queue_id, "resolved_applied")
        await interaction.followup.send("Aktion ausgeführt.", ephemeral=True)

        for child in self.children:
            child.disabled = True
        try:
            if interaction.message:
                await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass


async def setup(bot: ModerationBot) -> None:
    await bot.add_cog(ModerationCog(bot))
