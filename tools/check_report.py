"""Check report evidence, formatting and the Word-exported PDF."""
import json
import re
from pathlib import Path

import fitz
import numpy as np
import pandas as pd
from docx import Document

BASE=Path(__file__).resolve().parents[1]
text=(BASE/'docs/report.md').read_text(encoding='utf-8')
refs=re.findall(r'^@ref (\d+\.\d+)',text,re.M)
citations=re.findall(r'\[(\d+\.\d+)\]',text)
assert refs==list(dict.fromkeys(citations)), 'Sources must be cited in order'
assert len(refs)>=10
for path in re.findall(r'^@figure (.*?) \|',text,re.M):assert (BASE/'docs'/path).is_file()

doc=Document(BASE/'docs/report.docx')
for sec in doc.sections:
    for value,expected in [(sec.page_width.cm,21),(sec.page_height.cm,29.7),
                           (sec.left_margin.cm,3),(sec.right_margin.cm,1),
                           (sec.top_margin.cm,2),(sec.bottom_margin.cm,2)]:
        assert abs(value-expected)<.02
body=doc.styles['GP Body']
assert body.font.name=='Times New Roman' and body.font.size.pt==14
assert body.paragraph_format.line_spacing==1.5
assert abs(body.paragraph_format.first_line_indent.cm-1.25)<.01
for level,size in [(1,18),(2,16),(3,14)]:
    s=doc.styles[f'GP Heading {level}']
    assert s.font.size.pt==size and s.font.bold
    assert abs(s.paragraph_format.space_after.pt-28.35)<.1
    before=s.paragraph_format.space_before
    assert abs((before.pt if before is not None else 0)-(0 if level==1 else 42.55))<.1
assert len(doc.element.body.xpath('.//m:oMath'))==4
assert not doc.element.body.xpath('.//w:footnoteReference')
assert not doc.sections[0].footer._element.xpath('.//w:instrText')
assert any('PAGE' in (e.text or '') for e in doc.sections[1].footer._element.xpath('.//w:instrText'))

manifest=json.loads((BASE/'docs/report-validation.json').read_text(encoding='utf-8'))
rows=pd.read_csv(BASE/'docs/report-validation.csv')
test=rows.iloc[manifest['calibration_hours']:]
for name in ('boosting','seasonal'):
    residual=(test.actual-test[name]).to_numpy()
    m=manifest['models'][name]
    assert m['n']==len(test)==336
    assert np.isclose(np.abs(residual).mean(),m['mae_mw'])
    assert np.isclose(np.sqrt((residual**2).mean()),m['rmse_mw'])
    assert np.isclose((np.abs(residual)/test.actual.to_numpy()).mean()*100,m['mape_pct'])
    assert np.isclose((np.abs(residual)<=m['interval_halfwidth_mw']).mean()*100,m['coverage_90_pct'])

pdf=fitz.open(BASE/'docs/report.pdf')
assert len(pdf)>20
pdftext='\n'.join(page.get_text() for page in pdf)
assert 'Элементы оглавления не найдены' not in pdftext
assert 'Содержание обновляется' not in pdftext
assert 'ВВЕДЕНИЕ' in pdf[1].get_text()
assert 'ВВЕДЕНИЕ' in pdf[2].get_text()
for i,page in enumerate(pdf):
    spans=[s for b in page.get_text('dict')['blocks'] if 'lines' in b for line in b['lines'] for s in line['spans']]
    footer=[s for s in spans if s['bbox'][1]>785 and s['text'].strip().isdigit()]
    if i<2:assert not footer
    else:
        assert any(s['text'].strip()==str(i+1) and abs(s['size']-11)<.1 for s in footer),f'Footer on page {i+1}'
    assert page.get_text().strip() or page.get_images(),f'Blank page {i+1}'
for number in refs:assert f'[{number}]' in pdftext
print(json.dumps({'status':'ok','pages':len(pdf),'sources':len(refs),'figures':len(re.findall(r'^@figure ',text,re.M)),
                  'word_equations':4,'test_rows':len(test),'metrics_recomputed':True},ensure_ascii=True))
