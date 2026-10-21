"""Locate the same draft fields in the upstream table or optional detail editor."""
def editor_fields(page, index=None):
    detail=page.get_by_test_id('asr-segment-text')
    if detail.count():
        return detail,page.get_by_test_id('asr-start'),page.get_by_test_id('asr-end')
    row=page.locator('tr[data-sample-range=true]').first if index is None else page.locator(f'tr[data-segment-index="{index}"]')
    return row.locator('textarea'),row.locator('input[type=number]').nth(0),row.locator('input[type=number]').nth(1)
