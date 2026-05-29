import { useEffect, useState } from 'react'

// In-browser DuckDB-Wasm — lazy-loaded so the main dashboard bundle stays small.
// First use fetches the wasm bundle from jsDelivr (~10 MB); subsequent hooks reuse
// the same AsyncDuckDB instance.

let _dbPromise = null

async function _initDB() {
  const duckdb = await import('@duckdb/duckdb-wasm')  // ← dynamic import
  const bundles = duckdb.getJsDelivrBundles()
  const bundle = await duckdb.selectBundle(bundles)
  const workerUrl = URL.createObjectURL(
    new Blob([`importScripts("${bundle.mainWorker}");`], { type: 'text/javascript' }),
  )
  const worker = new Worker(workerUrl)
  const db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), worker)
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker)
  URL.revokeObjectURL(workerUrl)
  return db
}

export function useDuckDB(enabled = true) {
  const [db, setDb] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!enabled) return
    if (!_dbPromise) _dbPromise = _initDB()
    let cancelled = false
    _dbPromise.then(d => { if (!cancelled) setDb(d) })
              .catch(e => { if (!cancelled) setError(String(e)) })
    return () => { cancelled = true }
  }, [enabled])

  return { db, error }
}

// Convert an Arrow Table from DuckDB into the {columns, rows} shape the page uses.
export function arrowToRows(arrowTable) {
  const columns = arrowTable.schema.fields.map(f => f.name)
  const rows = []
  for (const row of arrowTable) {
    const obj = row.toJSON()
    rows.push(columns.map(c => {
      const v = obj[c]
      // Arrow BigInts -> Number for display; DuckDB types like Decimal end up as strings already.
      return typeof v === 'bigint' ? Number(v) : v
    }))
  }
  return { columns, rows }
}
