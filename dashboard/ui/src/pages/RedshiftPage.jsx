import { useState, useEffect } from 'react'
import Header from '@cloudscape-design/components/header'
import Table from '@cloudscape-design/components/table'
import Box from '@cloudscape-design/components/box'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Container from '@cloudscape-design/components/container'
import Button from '@cloudscape-design/components/button'
import Tabs from '@cloudscape-design/components/tabs'
import Modal from '@cloudscape-design/components/modal'
import FormField from '@cloudscape-design/components/form-field'
import Input from '@cloudscape-design/components/input'
import Alert from '@cloudscape-design/components/alert'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import Select from '@cloudscape-design/components/select'
import ExpandableSection from '@cloudscape-design/components/expandable-section'
import SqlEditor from '../components/SqlEditor'

const API = 'http://localhost:8000'
const HISTORY_KEY = 'oblako-redshift-history'
const HISTORY_MAX = 50

function loadHistory() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]') } catch { return [] }
}
function pushHistory(sql) {
  const cur = loadHistory().filter(q => q !== sql)
  cur.unshift(sql)
  const trimmed = cur.slice(0, HISTORY_MAX)
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(trimmed)) } catch {}
  return trimmed
}

let _tabCounter = 1
const newTab = (sql = '-- write SQL here\nSELECT 1 AS hello') => ({
  id: `t${_tabCounter++}`, title: `Query ${_tabCounter - 1}`, sql, result: null, loading: false,
})

function QueryTab({ tab, onChange, onRun }) {
  return (
    <SpaceBetween size="m">
      <SqlEditor value={tab.sql} onChange={sql => onChange({ ...tab, sql })} rows={8} />
      <Button variant="primary" onClick={() => onRun(tab)} loading={tab.loading}>Run</Button>
      {tab.result?.error && (
        <Alert type="error">{tab.result.error}</Alert>
      )}
      {tab.result?.message && (
        <Box color="text-status-success">{tab.result.message}</Box>
      )}
      {tab.result?.columns && (
        <Table
          header={<Header variant="h3" counter={`(${tab.result.rows.length})`}>Results</Header>}
          items={tab.result.rows}
          columnDefinitions={tab.result.columns.map(col => ({
            id: col, header: col,
            cell: item => <Box variant="code" fontSize="body-s">{String(item[col] ?? '')}</Box>,
          }))}
          empty={<Box textAlign="center">No rows.</Box>}
        />
      )}
    </SpaceBetween>
  )
}

export default function RedshiftPage() {
  const [clusters, setClusters] = useState([])
  const [tree, setTree] = useState([])
  const [tabs, setTabs] = useState([newTab()])
  const [activeId, setActiveId] = useState(null)
  const [history, setHistory] = useState(loadHistory())

  // Create-cluster modal state
  const [show, setShow] = useState(false)
  const [creating, setCreating] = useState(false)
  const [err, setErr] = useState(null)
  const [cform, setCform] = useState({
    clusterIdentifier: '', nodeType: 'ra3.xlplus', numberOfNodes: '1',
    dbName: 'dev', masterUsername: 'admin', masterUserPassword: 'Password123',
  })
  const setC = (k, v) => setCform(f => ({ ...f, [k]: v }))

  useEffect(() => {
    fetch(`${API}/api/redshift/clusters`).then(r => r.json()).then(d => setClusters(d.clusters || []))
    fetch(`${API}/api/redshift/schema`).then(r => r.json()).then(d => setTree(d.schemas || []))
    if (!activeId && tabs.length) setActiveId(tabs[0].id)
  }, [])

  const updateTab = (t) => setTabs(curr => curr.map(x => x.id === t.id ? t : x))

  const closeTab = (id) => {
    setTabs(curr => {
      const next = curr.filter(t => t.id !== id)
      if (id === activeId && next.length) setActiveId(next[next.length - 1].id)
      return next.length ? next : [newTab()]
    })
  }

  const addTab = (sql) => {
    const t = newTab(sql ?? '')
    setTabs(curr => [...curr, t]); setActiveId(t.id)
  }

  const runQuery = (tab) => {
    updateTab({ ...tab, loading: true, result: null })
    fetch(`${API}/api/redshift/query`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: tab.sql }),
    }).then(r => r.json()).then(d => {
      updateTab({ ...tab, loading: false, result: d })
      setHistory(pushHistory(tab.sql))
    }).catch(e => updateTab({ ...tab, loading: false, result: { error: String(e) } }))
  }

  const insertSelect = (schema, table) => {
    const current = tabs.find(t => t.id === activeId) || tabs[0]
    const sql = `SELECT * FROM ${schema}.${table} LIMIT 50`
    updateTab({ ...current, sql })
  }

  const useHistory = (sql) => {
    const current = tabs.find(t => t.id === activeId) || tabs[0]
    updateTab({ ...current, sql })
  }

  const createCluster = () => {
    if (!cform.clusterIdentifier) { setErr('Cluster identifier is required'); return }
    setCreating(true); setErr(null)
    fetch(`${API}/api/redshift/clusters`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cform),
    }).then(r => r.json()).then(d => {
      setCreating(false); if (d.error) { setErr(d.error); return } setShow(false)
      fetch(`${API}/api/redshift/clusters`).then(r => r.json()).then(x => setClusters(x.clusters || []))
    }).catch(e => { setCreating(false); setErr(String(e)) })
  }

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Query Editor v2 over the local pgredshift engine; psycopg dialect."
        actions={<Button iconName="add-plus" onClick={() => { setErr(null); setShow(true) }}>Create cluster</Button>}
      >
        Amazon Redshift
      </Header>

      <Table
        header={<Header variant="h2" counter={`(${clusters.length})`}>Clusters</Header>}
        items={clusters}
        columnDefinitions={[
          { id: 'id', header: 'Identifier', cell: c => <Box fontWeight="bold">{c.ClusterIdentifier}</Box> },
          { id: 'status', header: 'Status', cell: c => <StatusIndicator type={c.ClusterStatus === 'available' ? 'success' : 'in-progress'}>{c.ClusterStatus}</StatusIndicator> },
          { id: 'nodeType', header: 'Node type', cell: c => c.NodeType },
          { id: 'endpoint', header: 'Endpoint', cell: c => c.Endpoint?.Address ? `${c.Endpoint.Address}:${c.Endpoint.Port}` : '—' },
        ]}
      />

      <Container header={
        <Header variant="h2"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Select
                placeholder="History"
                selectedOption={null}
                options={history.slice(0, 20).map((q, i) => ({ value: String(i), label: q.split('\n')[0].slice(0, 80) || `(query ${i+1})`, description: q.length > 80 ? `${q.length} chars` : undefined }))}
                onChange={({ detail }) => useHistory(history[parseInt(detail.selectedOption.value, 10)])}
                empty="No queries yet"
              />
              <Button iconName="add-plus" onClick={() => addTab()}>New tab</Button>
            </SpaceBetween>
          }
        >Query editor</Header>
      }>
        <Tabs
          activeTabId={activeId}
          onChange={({ detail }) => setActiveId(detail.activeTabId)}
          tabs={tabs.map(t => ({
            id: t.id,
            label: t.title,
            action: tabs.length > 1
              ? <Button iconName="close" variant="icon" ariaLabel={`Close ${t.title}`} onClick={() => closeTab(t.id)} />
              : undefined,
            content: <QueryTab tab={t} onChange={updateTab} onRun={runQuery} />,
          }))}
        />
      </Container>

      <ExpandableSection headerText={`Schema browser (${tree.length} schema${tree.length === 1 ? '' : 's'})`} defaultExpanded>
        <SpaceBetween size="s">
          {tree.map(s => (
            <ExpandableSection key={s.name} headerText={s.name}>
              <SpaceBetween size="xxs">
                {s.tables.map(t => (
                  <Box key={t.name}>
                    <Button variant="link" onClick={() => insertSelect(s.name, t.name)}>{t.name}</Button>
                    {' '}
                    <Box variant="span" color="text-status-inactive" fontSize="body-s">
                      ({t.columns.map(c => `${c.name}:${c.type}`).join(', ')})
                    </Box>
                  </Box>
                ))}
                {!s.tables.length && <Box color="text-status-inactive">No tables.</Box>}
              </SpaceBetween>
            </ExpandableSection>
          ))}
          {!tree.length && <Box color="text-status-inactive">No schemas yet — create some tables first.</Box>}
        </SpaceBetween>
      </ExpandableSection>

      <Modal visible={show} onDismiss={() => setShow(false)} header="Create Redshift cluster"
        footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => setShow(false)}>Cancel</Button>
          <Button variant="primary" loading={creating} onClick={createCluster}>Create</Button>
        </SpaceBetween></Box>}>
        <SpaceBetween size="m">
          {err && <Alert type="error">{err}</Alert>}
          <FormField label="Cluster identifier"><Input value={cform.clusterIdentifier} onChange={({ detail }) => setC('clusterIdentifier', detail.value)} placeholder="analytics-cluster" /></FormField>
          <FormField label="Node type"><Input value={cform.nodeType} onChange={({ detail }) => setC('nodeType', detail.value)} /></FormField>
          <FormField label="Number of nodes"><Input type="number" value={cform.numberOfNodes} onChange={({ detail }) => setC('numberOfNodes', detail.value)} /></FormField>
          <FormField label="Database name"><Input value={cform.dbName} onChange={({ detail }) => setC('dbName', detail.value)} /></FormField>
          <FormField label="Master username"><Input value={cform.masterUsername} onChange={({ detail }) => setC('masterUsername', detail.value)} /></FormField>
          <FormField label="Master password"><Input type="password" value={cform.masterUserPassword} onChange={({ detail }) => setC('masterUserPassword', detail.value)} /></FormField>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  )
}
