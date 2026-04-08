from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from mcd_agent.models import MauticInstall


_PAGE_MODEL_REL_PATHS = [
    "app/bundles/PageBundle/Model/PageModel.php",
    "docroot/app/bundles/PageBundle/Model/PageModel.php",
    "public/app/bundles/PageBundle/Model/PageModel.php",
]
_PAGEHIT_HANDLER_REL_PATHS = [
    "app/bundles/MessengerBundle/MessageHandler/PageHitNotificationHandler.php",
    "docroot/app/bundles/MessengerBundle/MessageHandler/PageHitNotificationHandler.php",
    "public/app/bundles/MessengerBundle/MessageHandler/PageHitNotificationHandler.php",
]
_PATCH_MARKER = "mcd pagehit cascade patch"

_PAGE_MODEL_NEEDLE = """// Wrap in a try/catch to prevent deadlock errors on busy servers
        try {
            $this->em->persist($hit);
            $this->em->flush();
        } catch (\\Exception $exception) {
            if (MAUTIC_ENV !== 'prod') {
                throw $exception;
            } else {
                $this->logger->error(
                    $exception->getMessage(),
                    ['exception' => $exception]
                );
            }
        }

        """
_PAGE_MODEL_REPLACEMENT = """// mcd pagehit cascade patch begin
        $hitPersisted = false;
        try {
            $this->em->persist($hit);
            $this->em->flush();
            $hitPersisted = null !== $hit->getId();
        } catch (\\Exception $exception) {
            if (MAUTIC_ENV !== 'prod') {
                throw $exception;
            } else {
                $this->logger->error(
                    $exception->getMessage(),
                    ['exception' => $exception]
                );
            }
        }

        if (!$hitPersisted || !$hit->getId()) {
            return true;
        }
        // mcd pagehit cascade patch end

        """

_HANDLER_INVOKE_NEEDLE = """public function __invoke(PageHitNotification $message, Acknowledger $ack = null): void
    {
        $parsed = $this->parseMessage($message);
        $this->pageModel->processPageHit(...$parsed);
        $this->logger->info('processed page hit #'.$message->getHitId());
    }
"""
_HANDLER_INVOKE_REPLACEMENT = """public function __invoke(PageHitNotification $message, Acknowledger $ack = null): void
    {
        try {
            $parsed = $this->parseMessage($message);
        } catch (InvalidPayloadException $exception) {
            $this->logger->warning('Skipping invalid page hit notification: '.$exception->getMessage(), ['message' => $message]);

            return;
        }

        if (!isset($parsed['hit']) || !$parsed['hit'] instanceof Hit) {
            $this->logger->warning('Skipping page hit notification without persisted hit #'.$message->getHitId(), ['message' => $message]);

            return;
        }

        // mcd pagehit cascade patch
        $this->pageModel->processPageHit(...$parsed);
        $this->logger->info('processed page hit #'.$message->getHitId());
    }
"""


def _candidate_file(root: str, rel_paths: list[str]) -> Path | None:
    base = Path(root)
    for rel in rel_paths:
        p = base / rel
        if p.exists() and p.is_file():
            return p
    return None


def _patch_backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".mcd-bak")


def patch_status(install: MauticInstall) -> dict[str, Any]:
    page_model = _candidate_file(install.root, _PAGE_MODEL_REL_PATHS)
    handler = _candidate_file(install.root, _PAGEHIT_HANDLER_REL_PATHS)
    if not page_model or not handler:
        return {"status": "skip", "reason": "files_not_found", "root": install.root}
    try:
        page_text = page_model.read_text(encoding="utf-8", errors="ignore")
        handler_text = handler.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"status": "error", "reason": f"read_failed: {e}", "root": install.root}
    if _PATCH_MARKER in page_text and _PATCH_MARKER in handler_text:
        return {
            "status": "patched",
            "root": install.root,
            "page_model": str(page_model),
            "handler": str(handler),
        }
    if _PAGE_MODEL_NEEDLE in page_text and _HANDLER_INVOKE_NEEDLE in handler_text:
        return {
            "status": "vulnerable",
            "root": install.root,
            "page_model": str(page_model),
            "handler": str(handler),
        }
    return {
        "status": "skip",
        "reason": "pattern_not_found",
        "root": install.root,
        "page_model": str(page_model),
        "handler": str(handler),
    }


def ensure_pagehit_cascade_patch(install: MauticInstall) -> dict[str, Any]:
    page_model = _candidate_file(install.root, _PAGE_MODEL_REL_PATHS)
    handler = _candidate_file(install.root, _PAGEHIT_HANDLER_REL_PATHS)
    if not page_model or not handler:
        return {"status": "skip", "reason": "files_not_found", "root": install.root}

    try:
        page_text = page_model.read_text(encoding="utf-8", errors="ignore")
        handler_text = handler.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"status": "error", "reason": f"read_failed: {e}", "root": install.root}

    if _PATCH_MARKER in page_text and _PATCH_MARKER in handler_text:
        return {"status": "already", "root": install.root, "page_model": str(page_model), "handler": str(handler)}

    if _PAGE_MODEL_NEEDLE not in page_text:
        return {"status": "skip", "reason": "page_model_pattern_not_found", "root": install.root}
    patched_page_text = page_text.replace(_PAGE_MODEL_NEEDLE, _PAGE_MODEL_REPLACEMENT, 1)

    if _HANDLER_INVOKE_NEEDLE not in handler_text:
        return {"status": "skip", "reason": "handler_pattern_not_found", "root": install.root}
    patched_handler_text = handler_text.replace(_HANDLER_INVOKE_NEEDLE, _HANDLER_INVOKE_REPLACEMENT, 1)

    try:
        for p, original, patched in (
            (page_model, page_text, patched_page_text),
            (handler, handler_text, patched_handler_text),
        ):
            st = p.stat()
            backup = _patch_backup_path(p)
            if not backup.exists():
                backup.write_text(original, encoding="utf-8")
                os.chmod(backup, st.st_mode)
                try:
                    os.chown(backup, st.st_uid, st.st_gid)
                except PermissionError:
                    pass
            p.write_text(patched, encoding="utf-8")
            os.chmod(p, st.st_mode)
            try:
                os.chown(p, st.st_uid, st.st_gid)
            except PermissionError:
                pass
    except Exception as e:
        return {"status": "error", "reason": f"write_failed: {e}", "root": install.root}

    logging.info("[%s] pagehit cascade patch applied", install.root)
    return {
        "status": "patched",
        "root": install.root,
        "page_model": str(page_model),
        "handler": str(handler),
    }
