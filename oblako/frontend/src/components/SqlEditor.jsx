import CodeEditor from './CodeEditor'

// Back-compat shim: SqlEditor is just CodeEditor pinned to SQL.
export default function SqlEditor(props) {
  return <CodeEditor language="sql" {...props} />
}
