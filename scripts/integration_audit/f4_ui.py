"""Reproduce F4 false finished status without network or production changes."""
import json
import sys
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from streamlit.testing.v1 import AppTest

def get(path,**params):
    if path=='/properties': return [{'id':'prop_audit','name':'Audit property'}]
    if path in ('/f4/insights','/f4/snapshots'): return []
    raise AssertionError((path,params))


def main():
    with patch('ui.api_client.get',side_effect=get), patch('ui.api_client.post',return_value={'status':'published','completed':False}) as post:
        at=AppTest.from_file(str(ROOT/'app/features/f4_review_analysis/ui_page.py')).run(timeout=10)
        assert not at.exception
        at.button[0].click().run(timeout=10)
        assert not at.exception
        print(json.dumps({'post_response':{'status':'published','completed':False},
                          'success_messages':[item.value for item in at.success],
                          'post_calls':post.call_count},indent=2))

if __name__ == '__main__':
    main()
