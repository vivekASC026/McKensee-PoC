/**
 * Settings › Layout approval: who approves file layouts, if anyone.
 *
 * With no second approver a layout writes files as soon as it is saved, and
 * no approval step is shown anywhere. Naming one turns approval on: a layout
 * then writes files only once approved, and the approval is recorded in that
 * name. Clearing the name turns it off again.
 */

import { useCallback, useEffect, useState } from "react";
import { Banner, Button } from "../components/primitives";
import { ApiError, api } from "../lib/api";

export function LayoutApproval() {
  const [approver, setApprover] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  // The setting file is there and cannot be read: approval stays required.
  const [unreadable, setUnreadable] = useState(false);

  useEffect(() => {
    let live = true;
    api
      .layoutApproval()
      .then((setting) => {
        if (!live) return;
        setApprover(setting.secondApprover ?? "");
        setUnreadable(setting.required && !setting.secondApprover);
        setLoaded(true);
      })
      .catch((e) => live && setError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, []);

  const save = useCallback(async () => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const setting = await api.setLayoutApproval(approver.trim() || null);
      setApprover(setting.secondApprover ?? "");
      setUnreadable(false);
      setNote(
        setting.required
          ? `Layouts now need approval by ${setting.secondApprover}.`
          : "Layouts no longer need approval.",
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [approver]);

  return (
    <section className="panel" aria-label="Layout approval">
      <header>
        <h2>Layout approval</h2>
      </header>
      {error && <Banner tone="alarm">{error}</Banner>}
      {unreadable && (
        <Banner tone="caution">
          The approval setting can't be read, so layouts need approval. Save to set it again.
        </Banner>
      )}
      {note && <Banner tone="affirm">{note}</Banner>}
      <div className="panel__actions">
        <label className="named">
          <span>Second approver</span>
          <input
            value={approver}
            onChange={(e) => setApprover(e.target.value)}
            placeholder="None"
            maxLength={100}
            disabled={!loaded}
          />
        </label>
        <Button variant="primary" onClick={save} busy={busy} disabled={!loaded}>
          Save
        </Button>
      </div>
    </section>
  );
}
