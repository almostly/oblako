import { useEffect, useRef } from 'react'
import Prism from 'prismjs'
import 'prismjs/components/prism-sql'
import 'prismjs/themes/prism.css'

// A SQL editor with Prism syntax highlighting overlaid on a transparent textarea.
// Reused by Athena + Redshift Query Editor v2.
export default function SqlEditor({ value, onChange, rows = 8 }) {
  const ref = useRef(null)
  useEffect(() => { if (ref.current) Prism.highlightElement(ref.current) }, [value])
  const minHeight = rows * 22
  return (
    <div style={{ position: 'relative', minHeight, borderRadius: 4, border: '1px solid #d5dbdb', overflow: 'hidden' }}>
      <pre aria-hidden="true" style={{
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
        fontSize: 13, lineHeight: '1.5', padding: 10, margin: 0,
        position: 'absolute', top: 0, left: 0, right: 0, bottom: 0,
        background: 'transparent', pointerEvents: 'none', zIndex: 1,
        whiteSpace: 'pre-wrap', wordWrap: 'break-word', overflow: 'auto',
      }}>
        <code ref={ref} className="language-sql">{value + '\n'}</code>
      </pre>
      <textarea
        value={value}
        onChange={e => onChange(e.target.value)}
        spellCheck={false}
        style={{
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
          fontSize: 13, lineHeight: '1.5', padding: 10, margin: 0,
          position: 'relative', zIndex: 2,
          width: '100%', minHeight, resize: 'vertical',
          background: 'transparent', color: 'transparent',
          caretColor: '#000', border: 'none', outline: 'none',
          whiteSpace: 'pre-wrap', wordWrap: 'break-word',
        }}
      />
    </div>
  )
}
