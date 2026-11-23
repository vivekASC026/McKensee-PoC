/**
 * Files as a source, under Layouts and samples.
 *
 * Folder names a folder on this machine. File profile reads every delimited or
 * fixed-width file in it -- its kind, its fields, their widths and types -- and
 * measures what each field holds, the way a database's columns are measured.
 * File layouts is where a layout is made from one of them and each field given
 * its rule.
 */

import { useCallback, useEffect, useState } from "react";
import { Banner, Button, Chip, Empty, formatCount, formatPercent } from "../components/primitives";
import { ApiError, api, type FileProfile, type FileSource as Source } from "../lib/api";
import "./Files.css";

/** Folder: the folder sample files are read from. */
export function FileSource() {
  const [source, setSource] = useState<Source | null>(null);
  const [folder, setFolder] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .fileSource()
      .then((s) => {
        if (!live) return;
        setSource(s);
        // A path typed before this arrived is kept, not replaced.
        setFolder((typed) => typed || (s.folder ?? ""));
      })
      .catch((e) => live && setError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, []);

  const use = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setSource(await api.setFileSource(folder.trim()));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [folder]);

  return (
    <section className="files">
      <header className="files__head">
        <h2>File source</h2>
        <p className="panel__hint">
          The folder containing the delimited and fixed-width files to profile and map.
        </p>
      </header>
      {error && <Banner tone="alarm">{error}</Banner>}
      <div className="panel__actions">
        <label className="named files__folder">
          <span>Folder path</span>
          <input
            value={folder}
            onChange={(e) => setFolder(e.target.value)}
            placeholder="C:\\data\\files"
          />
        </label>
        <Button variant="primary" onClick={use} busy={busy} disabled={!folder.trim()}>
          Connect folder
        </Button>
      </div>
      {source?.folder &&
        (source.files.length === 0 ? (
          <Empty title="No files found">
            {source.folder} contains no .txt, .csv or .dat files.
          </Empty>
        ) : (
          <table className="panel__table">
            <thead>
              <tr>
                <th>File</th>
                <th className="right">Size</th>
                <th>Modified</th>
              </tr>
            </thead>
            <tbody>
              {source.files.map((file) => (
                <tr key={file.name}>
                  <td className="mono">{file.name}</td>
                  <td className="right num">{formatCount(file.size)} bytes</td>
                  <td>{file.modified.slice(0, 16).replace("T", " ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </section>
  );
}

/** Discover & Profile, for files: each file's structure and what it holds. */
export function FilesProfile() {
  const [profiles, setProfiles] = useState<FileProfile[] | null>(null);
  const [folder, setFolder] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .fileProfiles()
      .then((payload) => {
        if (!live) return;
        setFolder(payload.folder);
        setProfiles(payload.files);
        setOpen(payload.files[0]?.name ?? null);
      })
      .catch((e) => live && setError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, []);

  if (error) return <Banner tone="alarm">{error}</Banner>;
  if (profiles === null) return null;
  if (!folder) {
    return (
      <Empty title="No folder set">
        Set the source folder on the Folder tab.
      </Empty>
    );
  }
  if (profiles.length === 0) {
    return <Empty title="No files found">{folder} contains no delimited or fixed-width files.</Empty>;
  }

  return (
    <section className="files">
      <header className="files__head">
        <h2>File profile</h2>
        <p className="panel__hint">
          Structure and field statistics for each file in {folder}.
        </p>
      </header>
      <ul className="files__list">
        {profiles.map((file) => (
          <li key={file.name} className="files__file">
            <button
              className="files__file-head"
              aria-expanded={open === file.name}
              onClick={() => setOpen(open === file.name ? null : file.name)}
            >
              <span className="mono">{file.name}</span>
              {file.error ? (
                <Chip tone="alarm" fill="outline">Unreadable</Chip>
              ) : (
                <>
                  <Chip fill="outline">
                    {file.kind === "fixed"
                      ? "Fixed width"
                      : `Delimited (${file.delimiter === "\t" ? "tab" : `"${file.delimiter}"`})`}
                  </Chip>
                  <span className="files__facts">
                    {file.fields.length} {file.fields.length === 1 ? "field" : "fields"} ·{" "}
                    {formatCount(file.row_count)} {file.row_count === 1 ? "row" : "rows"}
                  </span>
                </>
              )}
            </button>
            {open === file.name &&
              (file.error ? (
                <p className="panel__hint">{file.error}</p>
              ) : (
                <table className="panel__table files__fields">
                  <thead>
                    <tr>
                      <th>Field</th>
                      {file.kind === "fixed" && <th className="right">Start</th>}
                      <th className="right">Width</th>
                      <th>Type</th>
                      <th className="right">Empty</th>
                      <th className="right">Distinct</th>
                      <th>Format</th>
                      <th>Values</th>
                    </tr>
                  </thead>
                  <tbody>
                    {file.fields.map((field) => (
                      <tr key={field.name}>
                        <td className="mono">{field.name}</td>
                        {file.kind === "fixed" && (
                          <td className="right num">{field.start ?? "—"}</td>
                        )}
                        <td className="right num">{field.length ?? field.max_width ?? "—"}</td>
                        <td>{field.type}</td>
                        <td className="right num">{formatPercent(field.empty_rate ?? 0)}</td>
                        <td className="right num">{formatCount(field.distinct ?? 0)}</td>
                        <td className="mono">{field.date_format ?? ""}</td>
                        <td className="mono files__samples">
                          {(field.samples ?? []).join(", ")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ))}
          </li>
        ))}
      </ul>
    </section>
  );
}
