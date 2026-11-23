/*
  The SDM logo: a database with a dashed ring and a spark above it, then the
  name on two lines. Drawn rather than an image so it follows the theme: the
  navy turns to light ink in the dark theme, the green to a lighter green.
*/

export function LogoMark({ size = 46 }: { size?: number }) {
  return (
    <svg
      className="logo__mark"
      width={size}
      height={(size * 56) / 64}
      viewBox="0 0 64 56"
      aria-hidden="true"
      focusable="false"
    >
      {/* The database: its top, its sides and its base. */}
      <g fill="none" stroke="var(--brand-ink)" strokeWidth="4.5" strokeLinecap="round">
        <ellipse cx="28" cy="32" rx="22" ry="6.5" />
        <path d="M6 32v12c0 3.6 9.8 6.5 22 6.5s22-2.9 22-6.5V32" />
      </g>
      {/* The ring, dashed, and the spark at its edge. */}
      <ellipse
        cx="28"
        cy="20"
        rx="23"
        ry="7"
        fill="none"
        stroke="var(--brand-green)"
        strokeWidth="4.5"
        strokeLinecap="round"
        strokeDasharray="6 5"
      />
      <path
        d="M54 0 C55 5.6 57 7.6 63 8.6 C57 9.6 55 11.6 54 17.2 C53 11.6 51 9.6 45 8.6 C51 7.6 53 5.6 54 0 Z"
        fill="var(--brand-green)"
      />
    </svg>
  );
}

export function Logo() {
  return (
    <div className="logo" role="img" aria-label="SDM, Synthetic Data Management">
      <LogoMark />
      <span className="logo__name" aria-hidden="true">
        <span className="logo__line">Synthetic Data</span>
        <span className="logo__line logo__line--green">Management</span>
      </span>
    </div>
  );
}
