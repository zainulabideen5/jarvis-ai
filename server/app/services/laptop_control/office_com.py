"""Office automation via COM (win32com.client) — TRUE SILENT BACKGROUND.

Excel/Word/Outlook ko `Visible = False` set karke control karte hain —
koi window appear NAHI hoti, user disturbed nahi hota. Yeh Microsoft ka
own API hai (COM) — pyautogui/UIA se 10x more reliable for Office.

Key requirements:
    - Microsoft Office MUST be installed locally (Office 365 desktop OK)
    - Office 365 Web-only accounts: COM available NAHI hota — falls to UIA
    - pythoncom.CoInitialize() needed per thread (Windows COM threading)

Architecture:
    OfficeCOM.get()
        .excel_run(action, params)         silent Excel
        .word_run(action, params)          silent Word
        .outlook_run(action, params)       silent Outlook (or Graph API fallback)

Each method opens app via COM, performs action, cleans up. Memory-safe.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)


def _ensure_com_threading():
    """Initialize COM for the current thread. Required by win32com on
    non-MTA threads (everything except Tkinter main thread in some Python builds).
    Idempotent — calling multiple times is safe.
    """
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:
        pass


class OfficeCOM:
    """Singleton — Excel/Word/Outlook silent automation via COM.

    Each call spawns a FRESH out-of-process COM server (Office's hidden
    instance), does the work, closes cleanly. Never reuses a global Excel
    instance to avoid memory leaks + Visible=False contamination of user's
    visible Excel windows.
    """

    _instance: "OfficeCOM | None" = None

    @classmethod
    def get(cls) -> "OfficeCOM":
        if cls._instance is None:
            cls._instance = OfficeCOM()
        return cls._instance

    # ==================================================================
    # EXCEL
    # ==================================================================

    def excel_read_range(self, file_path: str, sheet: str = "", cell_range: str = "A1:Z100") -> dict:
        """Read a cell range from a workbook. Silent.

        Args:
            file_path: full path to .xlsx file
            sheet:     sheet name (default: first sheet)
            cell_range: A1-style range

        Returns: {ok, rows: [[...], [...], ...], range, sheet}
        """
        if not os.path.exists(file_path):
            return {"ok": False, "error": f"File not found: {file_path}"}
        _ensure_com_threading()
        try:
            import win32com.client
            excel = win32com.client.DispatchEx("Excel.Application")
            excel.Visible = False
            excel.DisplayAlerts = False
            try:
                wb = excel.Workbooks.Open(os.path.abspath(file_path), ReadOnly=True)
                try:
                    ws = wb.Sheets(sheet) if sheet else wb.ActiveSheet
                    used = ws.UsedRange
                    # If user-specified range goes beyond UsedRange, clamp
                    actual_range = ws.Range(cell_range)
                    val = actual_range.Value
                    # COM returns tuple of tuples; normalize to list of lists
                    if val is None:
                        rows = []
                    elif isinstance(val, tuple) and len(val) and isinstance(val[0], tuple):
                        rows = [list(r) for r in val]
                    elif isinstance(val, tuple):
                        rows = [list(val)]
                    else:
                        rows = [[val]]
                    return {
                        "ok": True,
                        "sheet": ws.Name,
                        "range": cell_range,
                        "rows": rows,
                        "row_count": len(rows),
                    }
                finally:
                    wb.Close(SaveChanges=False)
            finally:
                excel.Quit()
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    def excel_write_cells(self, file_path: str, sheet: str, updates: list[dict]) -> dict:
        """Write cells to a workbook. Silent. Saves on success.

        Args:
            updates: list of {cell: "A1", value: "..." } dicts

        Returns: {ok, written: N}
        """
        if not os.path.exists(file_path):
            return {"ok": False, "error": f"File not found: {file_path}"}
        if not updates:
            return {"ok": False, "error": "No updates provided"}
        _ensure_com_threading()
        try:
            import win32com.client
            excel = win32com.client.DispatchEx("Excel.Application")
            excel.Visible = False
            excel.DisplayAlerts = False
            try:
                wb = excel.Workbooks.Open(os.path.abspath(file_path))
                try:
                    ws = wb.Sheets(sheet) if sheet else wb.ActiveSheet
                    written = 0
                    for u in updates:
                        cell = (u.get("cell") or "").strip()
                        val = u.get("value")
                        if not cell:
                            continue
                        ws.Range(cell).Value = val
                        written += 1
                    wb.Save()
                    return {"ok": True, "written": written, "sheet": ws.Name}
                finally:
                    # Already saved via wb.Save() above — close without
                    # SaveChanges to avoid Excel's "do you want to save?"
                    # prompt + double-flush data corruption risk on large files.
                    wb.Close(SaveChanges=False)
            finally:
                excel.Quit()
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    def excel_run_formula(self, file_path: str, sheet: str, target_cell: str, formula: str) -> dict:
        """Write a formula to a cell + return the calculated result. Silent.

        e.g. target_cell="A11", formula="=SUM(A1:A10)"
        """
        if not os.path.exists(file_path):
            return {"ok": False, "error": f"File not found: {file_path}"}
        _ensure_com_threading()
        try:
            import win32com.client
            excel = win32com.client.DispatchEx("Excel.Application")
            excel.Visible = False
            excel.DisplayAlerts = False
            try:
                wb = excel.Workbooks.Open(os.path.abspath(file_path))
                try:
                    ws = wb.Sheets(sheet) if sheet else wb.ActiveSheet
                    cell = ws.Range(target_cell)
                    cell.Formula = formula if formula.startswith("=") else f"={formula}"
                    excel.Calculate()
                    result = cell.Value
                    wb.Save()
                    return {"ok": True, "cell": target_cell, "formula": formula, "result": result}
                finally:
                    # Saved above — no SaveChanges to avoid double-flush
                    wb.Close(SaveChanges=False)
            finally:
                excel.Quit()
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    def excel_create_new(self, file_path: str, headers: list[str] | None = None, rows: list[list] | None = None) -> dict:
        """Create a NEW Excel file with optional headers + initial rows. Silent."""
        _ensure_com_threading()
        try:
            import win32com.client
            excel = win32com.client.DispatchEx("Excel.Application")
            excel.Visible = False
            excel.DisplayAlerts = False
            try:
                wb = excel.Workbooks.Add()
                try:
                    ws = wb.ActiveSheet
                    row_offset = 1
                    if headers:
                        for col_idx, h in enumerate(headers, start=1):
                            ws.Cells(1, col_idx).Value = h
                        row_offset = 2
                    if rows:
                        for row_idx, row in enumerate(rows, start=row_offset):
                            for col_idx, val in enumerate(row, start=1):
                                ws.Cells(row_idx, col_idx).Value = val
                    abs_path = os.path.abspath(file_path)
                    Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
                    wb.SaveAs(abs_path, FileFormat=51)  # 51 = xlOpenXMLWorkbook (.xlsx)
                    return {"ok": True, "path": abs_path, "rows_written": (len(rows) if rows else 0) + (1 if headers else 0)}
                finally:
                    wb.Close(SaveChanges=False)
            finally:
                excel.Quit()
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    def excel_append_row(self, file_path: str, sheet: str, row: list) -> dict:
        """Append a row at the bottom of the used range. Silent."""
        if not os.path.exists(file_path):
            return {"ok": False, "error": f"File not found: {file_path}"}
        _ensure_com_threading()
        try:
            import win32com.client
            excel = win32com.client.DispatchEx("Excel.Application")
            excel.Visible = False
            excel.DisplayAlerts = False
            try:
                wb = excel.Workbooks.Open(os.path.abspath(file_path))
                try:
                    ws = wb.Sheets(sheet) if sheet else wb.ActiveSheet
                    # Edge case: brand-new sheet — UsedRange.Rows.Count
                    # returns 1 even though no data exists. We should
                    # write to row 1 in that case, not row 2.
                    used = ws.UsedRange
                    try:
                        first_cell_blank = (used.Address == "$A$1" and ws.Cells(1, 1).Value is None)
                    except Exception:
                        first_cell_blank = False
                    next_row = 1 if first_cell_blank else (used.Rows.Count + 1)
                    for col_idx, val in enumerate(row, start=1):
                        ws.Cells(next_row, col_idx).Value = val
                    wb.Save()
                    return {"ok": True, "row_added": next_row}
                finally:
                    # Already saved; SaveChanges=False to avoid double-flush
                    wb.Close(SaveChanges=False)
            finally:
                excel.Quit()
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    # ==================================================================
    # WORD
    # ==================================================================

    def word_create(self, file_path: str, content: str, title: str = "") -> dict:
        """Create a Word document with text content. Silent."""
        _ensure_com_threading()
        try:
            import win32com.client
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = False
            try:
                doc = word.Documents.Add()
                try:
                    if title:
                        # Title style
                        title_para = doc.Paragraphs.Add()
                        title_para.Range.Text = title + "\n"
                        title_para.Range.Style = word.ActiveDocument.Styles("Title")
                    if content:
                        # Body
                        doc.Content.InsertAfter(content)
                    abs_path = os.path.abspath(file_path)
                    Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
                    doc.SaveAs(abs_path, FileFormat=16)  # 16 = wdFormatDocumentDefault (.docx)
                    return {"ok": True, "path": abs_path, "chars": len(content)}
                finally:
                    doc.Close(SaveChanges=False)
            finally:
                word.Quit()
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    def word_append(self, file_path: str, text: str) -> dict:
        """Append text to existing Word document. Silent."""
        if not os.path.exists(file_path):
            return {"ok": False, "error": f"File not found: {file_path}"}
        _ensure_com_threading()
        try:
            import win32com.client
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = False
            try:
                doc = word.Documents.Open(os.path.abspath(file_path))
                try:
                    doc.Content.InsertAfter("\n" + text)
                    doc.Save()
                    return {"ok": True, "appended_chars": len(text)}
                finally:
                    doc.Close(SaveChanges=False)
            finally:
                word.Quit()
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    def word_read(self, file_path: str) -> dict:
        """Read full text of a Word document. Silent."""
        if not os.path.exists(file_path):
            return {"ok": False, "error": f"File not found: {file_path}"}
        _ensure_com_threading()
        try:
            import win32com.client
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = False
            try:
                doc = word.Documents.Open(os.path.abspath(file_path), ReadOnly=True)
                try:
                    content = doc.Content.Text
                    return {"ok": True, "content": content, "chars": len(content)}
                finally:
                    doc.Close(SaveChanges=False)
            finally:
                word.Quit()
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    # ==================================================================
    # OUTLOOK
    # ==================================================================

    def outlook_list_accounts(self) -> dict:
        """List all email accounts configured in the user's Outlook profile.

        Returns each account's SMTP address, display name, and exchange type.
        Used by dashboard to let user label accounts (Personal/Work/Client A).
        """
        _ensure_com_threading()
        outlook = None
        try:
            import win32com.client
            # Pehle CHAL RAHE Outlook se attach karo (woh user ki LIVE profile +
            # saare accounts rakhta hai). Agar na chal raha ho to DispatchEx.
            try:
                outlook = win32com.client.GetActiveObject("Outlook.Application")
            except Exception:
                outlook = win32com.client.DispatchEx("Outlook.Application")
            session = outlook.Session
            accounts = []
            seen: set[str] = set()

            def _add(smtp, disp, user="", atype=0):
                smtp = (smtp or "").strip()
                disp = (disp or "").strip()
                key = (smtp or disp).lower()
                if not key or key in seen:
                    return
                seen.add(key)
                accounts.append({
                    "index": len(accounts) + 1, "smtp": smtp or disp,
                    "display_name": disp, "user_name": user,
                    "account_type": int(atype or 0),
                })

            # 1) Accounts collection (classic — configured send/receive accounts)
            try:
                for i in range(1, session.Accounts.Count + 1):
                    acc = session.Accounts.Item(i)
                    _add(getattr(acc, "SmtpAddress", ""),
                         getattr(acc, "DisplayName", ""),
                         getattr(acc, "UserName", ""),
                         getattr(acc, "AccountType", 0))
            except Exception as e:
                log.info("accounts_enum_partial", err=str(e)[:120])

            # 2) Stores (har account ka mailbox — kabhi Accounts se zyada milte hain)
            try:
                for j in range(1, session.Stores.Count + 1):
                    st = session.Stores.Item(j)
                    disp = getattr(st, "DisplayName", "") or ""
                    # Store ka SMTP nikalne ki koshish (account store ho to)
                    smtp = ""
                    try:
                        smtp = getattr(st, "SmtpAddress", "") or ""
                    except Exception:
                        smtp = ""
                    # DisplayName aksar email hota hai account-stores mein
                    _add(smtp or (disp if "@" in disp else ""), disp)
            except Exception as e:
                log.info("stores_enum_partial", err=str(e)[:120])

            return {"ok": True, "accounts": accounts, "count": len(accounts)}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300], "accounts": [], "count": 0}
        finally:
            # ALWAYS release COM ref — even if outlook creation itself succeeded
            # but a downstream call failed, we don't want zombie outlook.exe processes.
            if outlook is not None:
                try:
                    del outlook
                except Exception:
                    pass

    def outlook_send_email(
        self,
        to: str | list[str],
        subject: str,
        body: str,
        cc: str | list[str] = "",
        bcc: str | list[str] = "",
        attachments: list[str] | None = None,
        html_body: bool = False,
        from_account: str = "",
    ) -> dict:
        """Send an email via the user's local Outlook profile. Silent.

        `from_account` (optional): match against Outlook account SMTP address
        or display name (case-insensitive substring). If matched, the email
        is routed via SendUsingAccount = that account. If unmatched, the
        default account is used (with a warning in the response).
        """
        _ensure_com_threading()

        def _join(addrs):
            if isinstance(addrs, list):
                return "; ".join(a for a in addrs if a)
            return addrs or ""

        try:
            import win32com.client
            # CHAL RAHE Outlook se attach (user ki LIVE profile = saare accounts).
            # DispatchEx fresh/default profile kholta tha jisme aksar 1 hi account
            # hota → from_account match fail ho kar default se chali jaati thi.
            try:
                outlook = win32com.client.GetActiveObject("Outlook.Application")
            except Exception:
                outlook = win32com.client.DispatchEx("Outlook.Application")
            try:
                mail = outlook.CreateItem(0)  # 0 = olMailItem
                mail.To = _join(to)
                if cc:
                    mail.CC = _join(cc)
                if bcc:
                    mail.BCC = _join(bcc)
                mail.Subject = subject or "(no subject)"
                if html_body:
                    mail.HTMLBody = body or ""
                else:
                    mail.Body = body or ""
                for att in (attachments or []):
                    if att and os.path.exists(att):
                        mail.Attachments.Add(os.path.abspath(att))

                # Route via specific account if requested
                routed_via = ""
                from_warning = ""
                if from_account and from_account.strip():
                    needle = from_account.strip().lower()
                    matched_account = None
                    try:
                        session = outlook.Session
                        accs = [session.Accounts.Item(i)
                                for i in range(1, session.Accounts.Count + 1)]

                        def _smtp(a):
                            return (getattr(a, "SmtpAddress", "") or "").lower()

                        def _dn(a):
                            return (getattr(a, "DisplayName", "") or "").lower()

                        # exact smtp -> local-part/startswith -> substring (smtp ya display)
                        for a in accs:
                            if _smtp(a) == needle:
                                matched_account = a
                                break
                        if matched_account is None:
                            for a in accs:
                                if _smtp(a).split("@")[0] == needle or _smtp(a).startswith(needle):
                                    matched_account = a
                                    break
                        if matched_account is None:
                            for a in accs:
                                if needle in _smtp(a) or needle in _dn(a):
                                    matched_account = a
                                    break
                        if matched_account is not None:
                            routed_via = (getattr(matched_account, "SmtpAddress", "")
                                          or getattr(matched_account, "DisplayName", ""))
                    except Exception:
                        pass
                    if matched_account is not None:
                        try:
                            mail.SendUsingAccount = matched_account
                        except Exception as e:
                            from_warning = f"SendUsingAccount set fail: {e}"
                    else:
                        # Koi sendable ACCOUNT match nahi hua. Shayad yeh ek
                        # mailbox/STORE hai (jaise info@... jise user parh sakta hai
                        # par alag send-account nahi). Us address SE "Send-As /
                        # on-behalf" se bhejo — user ki apni mailbox pe permission
                        # hoti hai to chalega.
                        send_as = ""
                        try:
                            sess2 = outlook.Session
                            for k in range(1, sess2.Stores.Count + 1):
                                st = sess2.Stores.Item(k)
                                dn = getattr(st, "DisplayName", "") or ""
                                try:
                                    sm = getattr(st, "SmtpAddress", "") or ""
                                except Exception:
                                    sm = ""
                                cand = (sm or (dn if "@" in dn else "")).lower()
                                if cand and (cand == needle or needle in cand
                                             or cand.startswith(needle)):
                                    send_as = sm or dn
                                    break
                        except Exception:
                            pass
                        if not send_as and "@" in from_account:
                            send_as = from_account.strip()
                        if send_as:
                            try:
                                mail.SentOnBehalfOfName = send_as
                                # Send-As set ho gaya — jab woh mailbox Outlook mein
                                # active/added hota hai to email USI se jaati hai.
                                # routed_via set karo (success), koi scary warning nahi.
                                routed_via = f"{send_as} (send-as)"
                            except Exception as e:
                                from_warning = (f"'{from_account}' se nahi bhej paya "
                                                f"(send-as fail: {e}) — default use kiya")
                        else:
                            from_warning = (f"'{from_account}' Outlook accounts mein "
                                            "match nahi mila — default account use kiya")

                mail.Send()
                result = {"ok": True, "to": _join(to), "subject": subject}
                if routed_via:
                    result["from_account"] = routed_via
                if from_warning:
                    result["warning"] = from_warning
                return result
            finally:
                # Release the COM reference. We DON'T call Quit() because
                # that closes the user's Outlook window if they have it open.
                # Setting to None lets Python GC the COM ref so Outlook.exe
                # doesn't accumulate background processes per send.
                try:
                    del mail
                except Exception:
                    pass
                try:
                    del outlook
                except Exception:
                    pass
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    def outlook_recent_inbox(self, count: int = 10, folder: str = "Inbox") -> dict:
        """Read recent emails from Outlook Inbox. Silent."""
        _ensure_com_threading()
        outlook = None
        try:
            import win32com.client
            outlook = win32com.client.DispatchEx("Outlook.Application")
            namespace = outlook.GetNamespace("MAPI")
            inbox = namespace.GetDefaultFolder(6)  # 6 = olFolderInbox
            items = inbox.Items
            items.Sort("[ReceivedTime]", True)  # Sort newest first
            count = max(1, min(count, 50))
            out = []
            for i, msg in enumerate(items):
                if i >= count:
                    break
                try:
                    out.append({
                        "subject": str(msg.Subject)[:200] if msg.Subject else "",
                        "from": str(msg.SenderName)[:100] if hasattr(msg, "SenderName") else "",
                        "received": str(msg.ReceivedTime)[:30] if msg.ReceivedTime else "",
                        "unread": bool(msg.UnRead),
                        "preview": (str(msg.Body)[:200] if msg.Body else ""),
                    })
                except Exception:
                    continue
            return {"ok": True, "count": len(out), "messages": out}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}
        finally:
            if outlook is not None:
                try:
                    del outlook
                except Exception:
                    pass

    # ==================================================================
    # Diagnostics
    # ==================================================================

    def check_office_installed(self) -> dict:
        """Check which Office apps are available via COM."""
        _ensure_com_threading()
        results = {}
        for name, prog_id in (("excel", "Excel.Application"), ("word", "Word.Application"), ("outlook", "Outlook.Application")):
            try:
                import win32com.client
                app = win32com.client.DispatchEx(prog_id)
                try:
                    version = app.Version if hasattr(app, "Version") else "unknown"
                    results[name] = {"available": True, "version": str(version)}
                finally:
                    try:
                        app.Quit()
                    except Exception:
                        pass
            except Exception as e:
                results[name] = {"available": False, "error": str(e)[:150]}
        return {"ok": True, "office": results}
