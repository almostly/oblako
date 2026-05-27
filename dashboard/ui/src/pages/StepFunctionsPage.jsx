import { useState, useEffect, useRef } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import Button from '@cloudscape-design/components/button'
import Container from '@cloudscape-design/components/container'
import ColumnLayout from '@cloudscape-design/components/column-layout'
import Textarea from '@cloudscape-design/components/textarea'
import Modal from '@cloudscape-design/components/modal'
import Alert from '@cloudscape-design/components/alert'
import Badge from '@cloudscape-design/components/badge'
import ExpandableSection from '@cloudscape-design/components/expandable-section'
import Prism from 'prismjs'
import 'prismjs/components/prism-json'
import 'prismjs/themes/prism.css'
import FlowGraph from '../components/FlowGraph'

const API = 'http://localhost:8000'
const sleep = (ms) => new Promise(r => setTimeout(r, ms))

function JsonCode({ code }) {
  const ref = useRef(null)
  useEffect(() => { if (ref.current) Prism.highlightElement(ref.current) }, [code])
  return (
    <pre style={{ margin: 0, borderRadius: 6, overflow: 'auto', maxHeight: 360, background: '#f7f8fa', padding: 10 }}>
      <code ref={ref} className="language-json">{code}</code>
    </pre>
  )
}

const statusType = (s) => s === 'SUCCEEDED' ? 'success' : s === 'FAILED' ? 'error' : s === 'RUNNING' ? 'in-progress' : 'pending'

export default function StepFunctionsPage() {
  const [machines, setMachines] = useState([])
  const [templates, setTemplates] = useState([])
  const [selectedMachine, setSelectedMachine] = useState(null)
  const [machineDetail, setMachineDetail] = useState(null)
  const [executions, setExecutions] = useState([])
  const [loading, setLoading] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [creating, setCreating] = useState(null)
  const [running, setRunning] = useState(false)
  const [runResult, setRunResult] = useState(null)
  const [inputText, setInputText] = useState('{}')

  const fetchMachines = () => {
    setLoading(true)
    fetch(`${API}/api/stepfunctions/state-machines`)
      .then(r => r.json())
      .then(data => { setMachines(data.stateMachines || []); setLoading(false) })
      .catch(() => setLoading(false))
  }

  useEffect(() => {
    fetchMachines()
    fetch(`${API}/api/stepfunctions/templates`).then(r => r.json()).then(d => setTemplates(d.templates || []))
  }, [])

  const tplFor = (name) => templates.find(t => t.name === name)

  const openMachine = (arn, name) => {
    setSelectedMachine(arn); setRunResult(null)
    const tpl = tplFor(name)
    setInputText(JSON.stringify(tpl?.input ?? {}, null, 2))
    fetch(`${API}/api/stepfunctions/describe/${encodeURIComponent(arn)}`).then(r => r.json()).then(setMachineDetail)
    fetch(`${API}/api/stepfunctions/executions/${encodeURIComponent(arn)}`).then(r => r.json()).then(d => setExecutions(d.executions || []))
  }

  const createFromTemplate = (id) => {
    setCreating(id)
    fetch(`${API}/api/stepfunctions/create`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ templateId: id }),
    })
      .then(r => r.json())
      .then(d => { setCreating(null); setShowCreate(false); fetchMachines(); if (d.stateMachineArn) openMachine(d.stateMachineArn, d.name) })
      .catch(() => setCreating(null))
  }

  const runExecution = async (tpl) => {
    let input
    try { input = JSON.parse(inputText || '{}') }
    catch (e) { setRunResult({ status: 'FAILED', error: `Input is not valid JSON: ${e.message}`, steps: [] }); return }
    setRunning(true); setRunResult({ status: 'RUNNING', steps: [] })
    try {
      const started = await fetch(`${API}/api/stepfunctions/start`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stateMachineArn: selectedMachine, testCase: tpl?.testCase || null, input }),
      }).then(r => r.json())
      if (started.error) { setRunResult({ status: 'FAILED', error: started.error, steps: [] }); setRunning(false); return }
      let result = null
      for (let i = 0; i < 40; i++) {
        result = await fetch(`${API}/api/stepfunctions/execution/${encodeURIComponent(started.executionArn)}`).then(r => r.json())
        setRunResult(result)
        if (result.status && result.status !== 'RUNNING') break
        await sleep(600)
      }
      fetch(`${API}/api/stepfunctions/executions/${encodeURIComponent(selectedMachine)}`).then(r => r.json()).then(d => setExecutions(d.executions || []))
    } catch (e) {
      setRunResult({ status: 'FAILED', error: String(e), steps: [] })
    }
    setRunning(false)
  }

  // -- Detail view -------------------------------------------------------------------
  if (selectedMachine && machineDetail && !machineDetail.error) {
    const definition = machineDetail.definition || {}
    const tpl = tplFor(machineDetail.name)
    const statusByState = Object.fromEntries((runResult?.steps || []).map(s => [s.name, s.status]))
    let outputText = ''
    if (runResult?.output) { try { outputText = JSON.stringify(JSON.parse(runResult.output), null, 2) } catch { outputText = runResult.output } }

    return (
      <SpaceBetween size="l">
        <Header
          variant="h1"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="primary" iconName="play" loading={running} disabled={!tpl?.runnable}
                onClick={() => runExecution(tpl)}>
                {tpl?.execMode === 'live' ? 'Run (live)' : 'Run (mock)'}
              </Button>
              <Button onClick={() => { setSelectedMachine(null); setMachineDetail(null); setRunResult(null) }}>Back</Button>
            </SpaceBetween>
          }
          description={tpl?.note}
        >
          {machineDetail.name}
        </Header>

        <ColumnLayout columns={2}>
          <Container header={<Header variant="h2" description="Edit, then Run">Input</Header>}>
            <Textarea value={inputText} onChange={({ detail }) => setInputText(detail.value)} rows={12} spellcheck={false} />
          </Container>
          <Container header={
            <Header variant="h2" actions={runResult && <StatusIndicator type={statusType(runResult.status)}>{runResult.status}</StatusIndicator>}>
              Output
            </Header>
          }>
            {runResult?.error && <Alert type="error">{runResult.cause || runResult.error}</Alert>}
            {outputText
              ? <JsonCode code={outputText} />
              : <Box color="text-status-inactive" padding={{ vertical: 'l' }} textAlign="center">Run the state machine to see output.</Box>}
          </Container>
        </ColumnLayout>

        <Container header={<Header variant="h2" description="States colour by run status; live runs animate the active edge">Visual flow</Header>}>
          <FlowGraph definition={definition} statusByState={statusByState} />
        </Container>

        <Table
          header={<Header variant="h2" counter={`(${executions.length})`}>Execution runs</Header>}
          items={executions}
          columnDefinitions={[
            { id: 'name', header: 'Name', cell: i => <Box variant="code" fontSize="body-s">{i.name}</Box> },
            { id: 'status', header: 'Status', cell: i => <StatusIndicator type={statusType(i.status)}>{i.status}</StatusIndicator> },
            { id: 'started', header: 'Started', cell: i => i.startDate },
          ]}
          empty={<Box textAlign="center">No runs yet.</Box>}
        />

        <ExpandableSection headerText="ASL definition (JSON)">
          <JsonCode code={JSON.stringify(definition, null, 2)} />
        </ExpandableSection>
      </SpaceBetween>
    )
  }

  // -- List view ---------------------------------------------------------------------
  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button iconName="add-plus" onClick={() => setShowCreate(true)}>Create from template</Button>
            <Button onClick={fetchMachines} iconName="refresh">Refresh</Button>
          </SpaceBetween>
        }
      >
        AWS Step Functions
      </Header>

      <Table
        header={<Header variant="h2">State machines</Header>}
        loading={loading}
        items={machines}
        columnDefinitions={[
          { id: 'name', header: 'Name', cell: item => <Button variant="link" onClick={() => openMachine(item.stateMachineArn, item.name)}>{item.name}</Button> },
          { id: 'arn', header: 'ARN', cell: item => <Box variant="code" fontSize="body-s">{item.stateMachineArn}</Box> },
          { id: 'created', header: 'Created', cell: item => item.creationDate },
        ]}
        empty={<Box textAlign="center">No state machines yet. Create one from a template.</Box>}
      />

      <Modal visible={showCreate} onDismiss={() => setShowCreate(false)} header="Create a state machine from a template" size="large">
        <SpaceBetween size="m">
          <Box color="text-body-secondary">
            ML-focused credit-risk workflows adapted from aws-samples/credit-risk-modeling-on-aws.
            Mock templates execute via Step Functions Local mock mode; the Bedrock template runs live against your local model.
          </Box>
          {templates.map(t => (
            <Container
              key={t.id}
              header={
                <Header variant="h3"
                  actions={<Button variant="primary" loading={creating === t.id} onClick={() => createFromTemplate(t.id)}>Create</Button>}>
                  {t.name}{' '}
                  {t.execMode === 'live' ? <Badge color="blue">live</Badge> : <Badge color="green">mock-runnable</Badge>}
                </Header>
              }
            >
              <Box>{t.comment}</Box>
              {t.note && <Box color="text-status-inactive" fontSize="body-s" padding={{ top: 'xs' }}>{t.note}</Box>}
            </Container>
          ))}
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  )
}
