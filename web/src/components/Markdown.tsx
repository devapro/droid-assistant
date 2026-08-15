/**
 * A small Markdown renderer for artifact content.
 *
 * A full parser would be the single largest thing in the bundle (NFR-RES-6), and
 * artifacts come from prompts that ask for headings, lists, and emphasis. Text
 * is never injected as HTML, so a malformed artifact renders as plain text
 * rather than becoming a script (NFR-SEC-9).
 */
export function Markdown({ source }: { source: string }) {
  const lines = source.split('\n')
  return (
    <div className="max-w-prose text-[15px] leading-relaxed">
      {lines.map((line, index) => {
        if (/^#{1,6}\s/.test(line)) {
          const level = line.match(/^#+/)?.[0].length ?? 1
          const text = line.replace(/^#+\s*/, '')
          const size = level <= 2 ? 'text-lg font-semibold' : 'text-base font-semibold'
          return (
            <p key={index} className={`mt-4 mb-1 ${size}`}>
              {inline(text)}
            </p>
          )
        }
        if (/^\s*[-*]\s*\[[ x]\]\s/.test(line)) {
          const done = /\[x\]/i.test(line)
          return (
            <p key={index} className="flex gap-2 py-0.5">
              <span className={done ? 'text-good' : 'text-fg-dim'}>{done ? '☑' : '☐'}</span>
              <span>{inline(line.replace(/^\s*[-*]\s*\[[ x]\]\s*/i, ''))}</span>
            </p>
          )
        }
        if (/^\s*[-*]\s/.test(line)) {
          return (
            <p key={index} className="flex gap-2 py-0.5 pl-2">
              <span className="text-fg-dim">•</span>
              <span>{inline(line.replace(/^\s*[-*]\s*/, ''))}</span>
            </p>
          )
        }
        if (/^\s*>/.test(line)) {
          return (
            <p key={index} className="border-line text-fg-dim my-1 border-l-2 pl-3 text-sm italic">
              {inline(line.replace(/^\s*>\s?/, ''))}
            </p>
          )
        }
        if (!line.trim()) return <div key={index} className="h-2" />
        return (
          <p key={index} className="py-0.5">
            {inline(line)}
          </p>
        )
      })}
    </div>
  )
}

function inline(text: string): React.ReactNode {
  const parts = text.split(/(\*\*[^*]+\*\*|_[^_]+_|`[^`]+`)/g)
  return parts.map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return <strong key={index}>{part.slice(2, -2)}</strong>
    }
    if (part.startsWith('_') && part.endsWith('_') && part.length > 2) {
      return <em key={index}>{part.slice(1, -1)}</em>
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return (
        <code key={index} className="bg-surface-2 rounded px-1 py-0.5 font-mono text-[13px]">
          {part.slice(1, -1)}
        </code>
      )
    }
    return <span key={index}>{part}</span>
  })
}
