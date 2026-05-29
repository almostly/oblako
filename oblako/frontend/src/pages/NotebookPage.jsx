import { useState, useEffect, useRef } from 'react'
import Header from '@cloudscape-design/components/header'
import Container from '@cloudscape-design/components/container'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Box from '@cloudscape-design/components/box'
import Button from '@cloudscape-design/components/button'
import Select from '@cloudscape-design/components/select'
import ColumnLayout from '@cloudscape-design/components/column-layout'
import Tabs from '@cloudscape-design/components/tabs'
import Link from '@cloudscape-design/components/link'
import Spinner from '@cloudscape-design/components/spinner'
import Prism from 'prismjs'
import 'prismjs/components/prism-python'
import 'prismjs/themes/prism.css'

const API = 'http://localhost:8000'

const SNIPPETS = [
  {
    label: 'S3: List buckets',
    value: `s3 = oblako.s3.get_client()
for b in s3.list_buckets()["Buckets"]:
    print(b["Name"])`,
  },
  {
    label: 'DynamoDB: Scan table',
    value: `ddb = oblako.dynamodb.get_client()
tables = ddb.list_tables()["TableNames"]
print("Tables:", tables)
if tables:
    items = ddb.scan(TableName=tables[0], Limit=5)["Items"]
    for item in items:
        print({k: list(v.values())[0] for k, v in item.items()})`,
  },
  {
    label: 'Redshift: Query',
    value: `conn = oblako.redshift.connect()
conn.autocommit = True
cur = conn.cursor()
cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
for row in cur.fetchall():
    print(row[0])
cur.close()
conn.close()`,
  },
  {
    label: 'Bedrock: Chat with Ollama',
    value: `from oblako.bedrock.adapter import BedrockAdapter
adapter = BedrockAdapter()
result = adapter.converse(
    model_id="qwen2.5:0.5b",
    messages=[{"role": "user", "content": [{"text": "What is credit risk? One sentence."}]}],
    inference_config={"maxTokens": 64},
)
print(result["output"]["message"]["content"][0]["text"])
print(f"Tokens: {result['usage']['inputTokens']} in, {result['usage']['outputTokens']} out")`,
  },
  {
    label: 'SageMaker: List containers',
    value: `training = oblako.sagemaker.list_training_containers()
endpoints = oblako.sagemaker.list_endpoint_containers()
print(f"Training containers: {len(training)}")
print(f"Endpoint containers: {len(endpoints)}")
for c in training + endpoints:
    print(f"  {c['name']} ({c['status']})")`,
  },
  {
    label: 'Step Functions: List & execute',
    value: `sfn = oblako.stepfunctions.get_client()
machines = sfn.list_state_machines()["stateMachines"]
print(f"State machines: {len(machines)}")
for sm in machines:
    print(f"  {sm['name']}")
    execs = sfn.list_executions(stateMachineArn=sm["stateMachineArn"], maxResults=3)["executions"]
    for e in execs:
        print(f"    {e['name']}: {e['status']}")`,
  },
  {
    label: 'OpenSearch: List indices',
    value: `from opensearchpy import OpenSearch
client = OpenSearch(hosts=[{"host": "localhost", "port": 9200}], use_ssl=False)
indices = client.cat.indices(format="json")
for idx in indices:
    print(f"{idx['index']}: {idx['docs.count']} docs, {idx['store.size']}")`,
  },
]

function PythonCode({ code }) {
  const ref = useRef(null)
  useEffect(() => {
    if (ref.current) Prism.highlightElement(ref.current)
  }, [code])
  return (
    <pre style={{ margin: 0, borderRadius: 6, overflow: 'auto', maxHeight: 200 }}>
      <code ref={ref} className="language-python">{code}</code>
    </pre>
  )
}

const editorStyle = {
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
  fontSize: 13,
  lineHeight: '1.5',
  padding: 12,
  margin: 0,
  border: 'none',
  whiteSpace: 'pre-wrap',
  wordWrap: 'break-word',
  overflowWrap: 'break-word',
  tabSize: 4,
}

function CodeEditor({ value, onChange, rows = 12 }) {
  const highlightRef = useRef(null)

  useEffect(() => {
    if (highlightRef.current) Prism.highlightElement(highlightRef.current)
  }, [value])

  const minHeight = rows * 20

  return (
    <div style={{ position: 'relative', minHeight, borderRadius: 4, border: '1px solid #d5dbdb', overflow: 'hidden' }}>
      <pre aria-hidden="true" style={{
        ...editorStyle,
        position: 'absolute', top: 0, left: 0, right: 0, bottom: 0,
        background: 'transparent', pointerEvents: 'none', zIndex: 1,
        overflow: 'auto',
      }}>
        <code ref={highlightRef} className="language-python">
          {value + '\n'}
        </code>
      </pre>
      <textarea
        value={value}
        onChange={e => onChange(e.target.value)}
        spellCheck={false}
        autoCapitalize="off"
        autoComplete="off"
        autoCorrect="off"
        style={{
          ...editorStyle,
          position: 'relative', zIndex: 2,
          width: '100%', minHeight, resize: 'vertical',
          background: 'transparent', color: 'transparent',
          caretColor: '#16191f',
          outline: 'none',
        }}
      />
    </div>
  )
}

// Launches JupyterLab (pre-wired to oblako) and embeds it in an iframe.
function JupyterLabTab() {
  const [state, setState] = useState({ loading: true, url: null, error: null })

  useEffect(() => {
    let active = true
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setState({ loading: true, url: null, error: null })
    fetch(`${API}/api/notebook/launch`, { method: 'POST' })
      .then(r => r.json())
      .then(d => { if (active) setState({ loading: false, url: d.url || null, error: d.error || null }) })
      .catch(e => { if (active) setState({ loading: false, url: null, error: e.message }) })
    return () => { active = false }
  }, [])

  if (state.loading) {
    return <Box padding="l"><Spinner /> Launching JupyterLab… (the first launch builds the server)</Box>
  }
  if (state.error) {
    return <Box padding="l" color="text-status-error">{state.error}</Box>
  }
  return (
    <SpaceBetween size="xs">
      <Box float="right"><Link external href={state.url}>Open in a new tab</Link></Box>
      <iframe
        title="JupyterLab"
        src={state.url}
        style={{ width: '100%', height: '78vh', border: '1px solid #d5dbdb', borderRadius: 4 }}
      />
    </SpaceBetween>
  )
}

function CellRunner() {
  const [code, setCode] = useState(SNIPPETS[0].value)
  const [cells, setCells] = useState([])
  const [loading, setLoading] = useState(false)
  const [selectedSnippet, setSelectedSnippet] = useState({ label: SNIPPETS[0].label, value: '0' })

  const runCode = () => {
    if (!code.trim()) return
    setLoading(true)
    const cellCode = code
    fetch(`${API}/api/notebook/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: cellCode }),
    })
      .then(r => r.json())
      .then(data => {
        setCells(prev => [...prev, { code: cellCode, ...data }])
        setLoading(false)
      })
      .catch(e => {
        setCells(prev => [...prev, { code: cellCode, output: '', errors: e.message, status: 'error' }])
        setLoading(false)
      })
  }

  return (
    <SpaceBetween size="l">
      <Container header={<Header variant="h2" actions={
        <ColumnLayout columns={2}>
          <Select
            selectedOption={selectedSnippet}
            onChange={({ detail }) => {
              setSelectedSnippet(detail.selectedOption)
              setCode(SNIPPETS[parseInt(detail.selectedOption.value)].value)
            }}
            options={SNIPPETS.map((s, i) => ({ label: s.label, value: String(i) }))}
          />
          <Button variant="primary" onClick={runCode} loading={loading}>Run</Button>
        </ColumnLayout>
      }>Code</Header>}>
        <CodeEditor value={code} onChange={setCode} rows={12} />
        <Box variant="small" color="text-body-secondary" padding={{ top: 'xs' }}>
          Pre-loaded: oblako, boto3, json, pd (pandas). All services available via oblako.s3, oblako.redshift, etc.
        </Box>
      </Container>

      {[...cells].reverse().map((cell, i) => (
        <Container key={cells.length - 1 - i} header={
          <Header variant="h3">
            <Box color={cell.status === 'ok' ? 'text-status-success' : 'text-status-error'}>
              [{cells.length - i}] {cell.status === 'ok' ? 'OK' : 'Error'}
            </Box>
          </Header>
        }>
          <SpaceBetween size="s">
            <PythonCode code={cell.code} />
            {cell.output && (
              <pre style={{
                background: '#f4f4f4', padding: 12, borderRadius: 6,
                fontSize: 13, lineHeight: 1.5, overflow: 'auto', maxHeight: 300,
              }}>{cell.output}</pre>
            )}
            {cell.errors && (
              <pre style={{
                background: '#fff5f5', color: '#d13212', padding: 12, borderRadius: 6,
                fontSize: 13, lineHeight: 1.5, overflow: 'auto', maxHeight: 300,
              }}>{cell.errors}</pre>
            )}
          </SpaceBetween>
        </Container>
      ))}
    </SpaceBetween>
  )
}

export default function NotebookPage({ embedded = false }) {
  const [activeTabId, setActiveTabId] = useState('cells')

  const tabs = (
    <Tabs
      activeTabId={activeTabId}
      onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
      tabs={[
        { id: 'cells', label: 'Quick cells', content: <CellRunner /> },
        { id: 'jupyterlab', label: 'JupyterLab', content: <JupyterLabTab /> },
      ]}
    />
  )

  if (embedded) return tabs

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Run quick cells server-side, or open the full JupyterLab embedded here — its kernel is pre-wired so plain boto3 hits oblako."
      >
        Notebook
      </Header>
      {tabs}
    </SpaceBetween>
  )
}
