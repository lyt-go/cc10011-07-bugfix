import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dimensionmap'
const columns = [
  "规则编号",
  "原始维度名",
  "标准维度名",
  "映射类型",
  "值转换逻辑",
  "默认取值",
  "映射优先级",
  "最近更新时间"
]
const statuses = [
  "草稿",
  "规则评审中",
  "已生效",
  "已废弃"
]
const STATUS_ACTIONS: Record<string, string[]> = {
  "草稿": ["提交规则评审", "废弃规则"],
  "规则评审中": ["生效规则", "废弃规则"],
  "已生效": ["废弃规则"],
  "已废弃": [],
}

type Row = Record<string, any>

const CREATE_FIELDS = ["规则编号", "原始维度名", "标准维度名", "映射类型", "值转换逻辑", "默认取值", "映射优先级"]

export default function DimensionmapView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [keyword, setKeyword] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [form, setForm] = useState<Record<string, string>>({})
  const [submitting, setSubmitting] = useState(false)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams()
    if (keyword) query.set('keyword', keyword)
    if (statusFilter) query.set('status', statusFilter)
    query.set('size', '200')
    try {
      const response = await request(`${ENDPOINT}?${query.toString()}`)
      if (!response.ok) {
        throw new Error('映射规则列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '维度映射列表读取失败')
    }
  }, [keyword, statusFilter])

  useEffect(() => {
    void reload()
  }, [reload])

  const stats = useMemo(() => [
    { label: '映射规则总数', value: total },
    { label: '已生效规则', value: rows.filter((row) => row.status === '已生效').length },
    { label: '评审中规则', value: rows.filter((row) => row.status === '规则评审中').length },
  ], [rows, total])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(payload.detail ?? '维度映射动作未生效，请稍后重试')
      }
      if (payload.ok === false) {
        throw new Error(payload.message ?? '维度映射动作未生效')
      }
      setNotice(payload.message ?? `映射规则已${action}`)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '维度映射操作失败')
    }
  }

  async function submitCreate() {
    setSubmitting(true)
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({ values: { ...form } }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(payload.detail ?? '映射规则登记失败')
      }
      if (payload.ok === false) {
        throw new Error(payload.message ?? '映射规则登记失败')
      }
      setNotice(payload.message ?? '映射规则已登记')
      setShowCreate(false)
      setForm({})
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '映射规则登记失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="page" data-module="dimensionmap">
      <header className="page-head">
        <div>
          <h2>维度映射管理</h2>
          <p className="page-desc">管理分析维度的标准化映射规则，统一不同来源的维度命名和取值。规则生效时会同步写入标准维度映射结果，供多维下钻引用同一口径。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setShowCreate((prev) => !prev)}>
            {showCreate ? '收起登记' : '登记映射规则'}
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出维度映射清单
          </button>
        </div>
      </header>

      {showCreate && (
        <form
          className="create-panel"
          onSubmit={(event) => {
            event.preventDefault()
            void submitCreate()
          }}
        >
          <div className="create-row">
            {CREATE_FIELDS.map((field) => (
              <label key={field} className="filter-item">
                <span>{field}</span>
                <input
                  value={form[field] ?? ''}
                  placeholder={`请输入${field}`}
                  onChange={(event) => setForm((prev) => ({ ...prev, [field]: event.target.value }))}
                />
              </label>
            ))}
          </div>
          <div className="create-actions">
            <button className="btn primary" type="submit" disabled={submitting}>
              {submitting ? '提交中…' : '提交登记'}
            </button>
            <span className="hint-text">规则编号、原始维度名、标准维度名为必填；生效后同步写入标准维度映射结果</span>
          </div>
        </form>
      )}

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        <label className="filter-item">
          <span>规则编号</span>
          <input value={keyword} placeholder="按规则编号检索" onChange={(event) => setKeyword(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>状态</span>
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="">全部状态</option>
            {statuses.map((item) => (
              <option key={item} value={item}>{item}</option>
            ))}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => { setKeyword(''); setStatusFilter('') }}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{row.status}</td>
              <td className="row-actions">
                {(STATUS_ACTIONS[String(row.status)] ?? []).map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无维度映射数据，可先登记映射规则</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条维度映射记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && (
          <span className="error-text">
            {errorMessage}
            <button className="link" type="button" onClick={() => void reload()}>重试</button>
          </span>
        )}
      </footer>
    </section>
  )
}
