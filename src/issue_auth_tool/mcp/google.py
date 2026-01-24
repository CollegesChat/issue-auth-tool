from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


def google_search_snippets(query, api_key, cse_id, num=5):
    service = build('customsearch', 'v1', developerKey=api_key)
    try:
        res = (
            service.cse()
            .list(
                q=query,
                cx=cse_id,
                num=num,  # 最多一次请求能取多少条。默认最大是 10。
            )
            .execute()
        )
    except HttpError as e:
        print('请求出错：', e)
        return []

    items = res.get('items', [])
    results = []
    for item in items:
        title = item.get('title')
        link = item.get('link')
        snippet = item.get('snippet')
        results.append({'title': title, 'link': link, 'snippet': snippet})
    return results


def get_results(query: str, key: str, cse: str):  # 即 “cx”
    top5 = google_search_snippets(query, key, cse, num=5)
    result = ''
    for idx, r in enumerate(top5, start=1):
        result += f"""第 {idx} 条:标题：{r['title']}
链接：{r['link']}
摘要：{r['snippet']}"""
    return result
