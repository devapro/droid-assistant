/**
 * What a session cost, and on what (FR-CFG-7).
 *
 * A single total answers "how much" but not "why", which is the question an
 * operator actually has when a number looks wrong — recognition and translation
 * bill very differently, and Live mode over a cloud recogniser costs several
 * times what Balanced does.
 *
 * Every figure is labelled an estimate. It is computed from published list
 * prices and audio duration, so a negotiated rate, a minimum billing increment,
 * or a failed-but-billed request will all make it differ from the invoice.
 * Presenting it as the bill would be the wrong kind of confident.
 */

import { t } from '../i18n'

export function formatCost(usd: number): string {
  if (usd <= 0) return '$0.00'
  // Sub-cent totals are normal for a short session; rounding them to $0.00
  // would make the feature look broken.
  return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`
}

export function CostBreakdown({
  total,
  breakdown,
  ceiling,
}: {
  total: number
  breakdown: Record<string, number>
  ceiling?: number | null
}) {
  const strings = t()
  const rows = Object.entries(breakdown)
    .filter(([, amount]) => amount > 0)
    .sort((a, b) => b[1] - a[1])

  if (total <= 0 && rows.length === 0) return null

  return (
    <div className="text-fg-dim text-xs">
      <div className="text-fg mb-1 flex items-baseline justify-between gap-3">
        <span className="font-medium">{strings.session.cost(total)}</span>
        {ceiling ? <span className="text-fg-dim">of {formatCost(ceiling)}</span> : null}
      </div>
      {rows.length > 0 && (
        <ul className="mb-1 flex flex-col gap-0.5">
          {rows.map(([component, amount]) => (
            <li key={component} className="flex items-baseline justify-between gap-3">
              <span>{strings.session.costComponent[component] ?? component}</span>
              <span className="font-mono tabular-nums">{formatCost(amount)}</span>
            </li>
          ))}
        </ul>
      )}
      <p className="opacity-80">{strings.session.costEstimate}</p>
    </div>
  )
}
