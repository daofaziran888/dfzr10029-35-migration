import { expect, test } from '@playwright/test'

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (e) => { throw new Error(`页面脚本异常：${e.message}`) })
})

test('审计迁移：批次归档输入、重复提交去重、旧文本与新差异核对', async ({ page }) => {
  test.setTimeout(180_000)
  await page.goto('/audit')
  await expect(page.getByTestId('migration-panel')).toBeVisible()
  await expect(page.locator('tbody tr[data-testid^="row-"]').first()).toBeVisible()

  // 迁移前：旧记录只暴露旧版文本，不混出差异结构。
  await expect(page.getByTestId('row-1')).toContainText('旧版文本')

  const legacyCount = await page.locator('tbody tr[data-testid^="row-"]').count()
  await page.getByTestId('open-migrate').click()
  await page.getByTestId('migrate-label').fill('E2E 迁移批次')
  await page.getByTestId('submit-migrate').click()
  await expect(page.getByTestId('migrate-modal')).toHaveCount(0)

  const batchRow = page.locator('tr[data-testid^="batch-row-"]').first()
  await expect(batchRow).toHaveAttribute('data-batch-status', '已迁移')
  const batchNo = (await batchRow.locator('td').first().innerText()).trim()

  // 旧文本与新差异在同一批次下对照，操作人和对象引用都在核对里。
  await page.getByTestId(`view-batch-${batchNo}`).click()
  await expect(page.getByTestId('batch-detail-modal')).toBeVisible()
  await expect(page.getByTestId('batch-item-2')).toContainText('陈思远')
  await expect(page.getByTestId('batch-item-2')).toContainText('通过')
  await expect(page.getByTestId('batch-item-2')).toContainText('字段类型')
  await page.getByTestId('batch-detail-modal').getByRole('button', { name: '关闭' }).click()

  // 批次过滤只返回该批次的新格式记录。
  await page.getByTestId('filter-batch').selectOption(batchNo)
  await page.getByTestId('filter-version').selectOption('版本化差异')
  await page.getByTestId('query').click()
  await expect(page.locator('tbody tr[data-testid^="row-"]')).toHaveCount(legacyCount)
  await expect(page.getByTestId('row-2')).toContainText('版本化差异')
  await page.getByTestId('diff-2').click()
  await expect(page.getByTestId('diff-modal')).toBeVisible()
  await expect(page.getByTestId('diff-modal')).toContainText('字段类型')
  await expect(page.getByTestId('diff-modal')).toContainText('通过')
  await page.getByTestId('diff-modal').getByRole('button', { name: '关闭' }).click()

  // 重复提交不会产生第二份有效结果。
  const batchCount = await page.locator('tr[data-testid^="batch-row-"]').count()
  await page.getByTestId('open-migrate').click()
  await page.getByTestId('submit-migrate').click()
  await expect(page.getByTestId('batch-message')).toContainText('一份有效结果')
  await expect(page.locator('tr[data-testid^="batch-row-"]')).toHaveCount(batchCount)

  // 回滚后回到旧版文本，操作人和对象引用仍然保留。
  await page.getByTestId('refresh-batches').click()
  await page.getByTestId(`rollback-batch-${batchNo}`).click()
  await expect(page.getByTestId('batch-message')).toContainText('已回滚')
  await page.getByTestId('reset').click()
  await expect(page.getByTestId('row-2')).toContainText('旧版文本')
  await expect(page.getByTestId('row-2')).toContainText('陈思远')
})

test('审计迁移：新登记事件直接写入版本化差异', async ({ page }) => {
  test.setTimeout(120_000)
  await page.goto('/audit')
  await page.getByTestId('open-create').click()
  const recordNo = `AUDI-E2E-${Date.now()}`
  const values: Record<string, string> = {
    记录编号: recordNo,
    操作类型: '新增指标',
    操作人: '王若彤',
    操作对象: 'METR-0009 支付转化率',
    操作时间: '2026-11-17 10:00:00',
    变更内容: '指标名称: 支付率 -> 支付转化率',
    审计结果: '待复核',
    记录状态: '生效',
  }
  for (const [field, value] of Object.entries(values)) {
    await page.getByTestId(`create-${field}`).fill(value)
  }
  await page.getByTestId('submit-create').click()
  await expect(page.getByTestId('create-modal')).toHaveCount(0)

  const created = page.locator('tbody tr').filter({ hasText: recordNo }).first()
  await expect(created).toContainText('版本化差异')
  await expect(created).toContainText('指标名称: 支付率 -> 支付转化率')
  await created.getByRole('button', { name: '查看差异' }).click()
  await expect(page.getByTestId('diff-modal')).toContainText('版本化差异')
  await expect(page.getByTestId('diff-modal')).toContainText('王若彤')
})
