"""Conservative reading of noon GetContent; content status is not offer sellability."""
import json
QC={'QC_STATUS_NOT_ELIGIBLE':'内容未齐，尚不能审核','QC_STATUS_PENDING':'平台审核中','QC_STATUS_APPROVED':'内容审核通过','QC_STATUS_REJECTED':'内容审核未通过'}
OVERALL={'OVERALL_STATUS_ACTIVE':'内容状态有效','OVERALL_STATUS_INACTIVE':'内容状态未激活'}
REVIEW={'REVIEW_STATUS_PENDING':'图片审核中','REVIEW_STATUS_VALID':'图片审核通过','REVIEW_STATUS_INVALID':'图片审核未通过'}
VISIBILITY={'VISIBILITY_STATUS_VISIBLE':'平台标记可见','VISIBILITY_STATUS_HIDDEN':'平台标记隐藏'}
LANG={'LANGUAGE_EN':'英文','LANGUAGE_AR':'阿拉伯文'}

def code(value):return value if isinstance(value,str) else ''

def bad_lists(obj,keys):return any(k in obj and not isinstance(obj[k],list) for k in keys)

def details(value):
    if not isinstance(value,list):return []
    return [x if isinstance(x,str) else json.dumps(x,ensure_ascii=False,sort_keys=True) for x in value]

def summarize(platform,revision):
    p=platform if isinstance(platform,dict) else {}
    out={'group':'unsubmitted','label':'尚无平台编号','languages':[],'images':[],'notes':[],'live_verified':False}
    if not p.get('sku_parent'):return out
    out.update(group='unchecked',label='待回查内容状态',checked_at=p.get('checked_at',''),submitted_revision=p.get('submitted_revision'))
    if isinstance(p.get('submitted_revision'),int) and p['submitted_revision']!=revision:
        out['notes'].append('本地商品已修改；以下是平台已提交内容的状态，不代表当前本地版本。')
    elif not isinstance(p.get('submitted_revision'),int):out['notes'].append('旧记录没有提交版本，无法确认与当前本地内容一致。')
    raw=p.get('content_response')
    if raw is None:return out
    out.update(group='unknown',label='平台结果待核对')
    if not isinstance(raw,dict) or raw.get('sku_parent')!=p['sku_parent']:
        out['notes'].append('返回的商品编号不匹配或缺失。');return out
    statuses=raw.get('statuses');images=raw.get('images');unknown=False;problem=False;pending=False
    if not isinstance(statuses,list) or not statuses:out['notes'].append('没有返回语言审核状态。');unknown=True;statuses=[]
    if not isinstance(images,list):out['notes'].append('没有返回图片审核列表。');unknown=True;images=[]
    eligible=False
    for index,im in enumerate(images):
        if not isinstance(im,dict):unknown=True;continue
        review=code(im.get('review_status'));visibility=code(im.get('visibility'));reasons=details(im.get('issues',[]))
        if review not in REVIEW or visibility not in VISIBILITY or bad_lists(im,['issues']):unknown=True
        if review=='REVIEW_STATUS_INVALID' or visibility=='VISIBILITY_STATUS_HIDDEN' or reasons:problem=True
        if review=='REVIEW_STATUS_PENDING':pending=True
        if review=='REVIEW_STATUS_VALID' and visibility=='VISIBILITY_STATUS_VISIBLE':eligible=True
        out['images'].append({'number':index+1,'review':REVIEW.get(review,'未知图片审核状态'),'visibility':VISIBILITY.get(visibility,'图片可见性未知'),'reasons':reasons})
    if not images:out['notes'].append('尚无可核对的图片。');problem=True
    if not eligible:out['notes'].append('尚未确认有审核通过且可见的图片。')
    seen=set();all_active=bool(statuses)
    for item in statuses:
        if not isinstance(item,dict):unknown=True;all_active=False;continue
        language=item.get('language');content=item.get('content');qc=item.get('qc');overall=code(item.get('overall_status'))
        if not isinstance(language,str):language=''
        if language not in LANG or language in seen:unknown=True
        seen.add(language)
        content=content if isinstance(content,dict) else {};qc=qc if isinstance(qc,dict) else {}
        status=code(qc.get('status'));complete=content.get('completeness');complete=complete if isinstance(complete,str) else ''
        reasons=[]
        reasons+=['缺少属性：'+x for x in details(content.get('missing_attributes',[]))]
        reasons+=['属性无效：'+x for x in details(content.get('invalid_attributes',[]))]
        reasons+=['审核原因：'+x for x in details(qc.get('rejection_reasons',[]))]
        reasons+=['平台错误：'+x for x in details(item.get('errors',[]))]
        if isinstance(qc.get('comment'),str) and qc['comment']:reasons.append('审核备注：'+qc['comment'])
        if status not in QC or overall not in OVERALL or not complete or bad_lists(content,['missing_attributes','invalid_attributes']) or bad_lists(qc,['rejection_reasons']) or bad_lists(item,['errors']):unknown=True
        if reasons or status in ('QC_STATUS_REJECTED','QC_STATUS_NOT_ELIGIBLE') or (complete and complete!='100%'):problem=True
        if status=='QC_STATUS_PENDING':pending=True
        active=overall=='OVERALL_STATUS_ACTIVE' and status=='QC_STATUS_APPROVED' and complete=='100%' and not reasons
        all_active=all_active and active
        out['languages'].append({'language':LANG.get(language,language or '未知语言'),'completeness':complete or '未知','qc':QC.get(status,'未知审核状态'),'overall':OVERALL.get(overall,'未知内容状态'),'reasons':reasons})
    missing=[label for code,label in LANG.items() if code not in seen]
    if missing:out['notes'].append('未返回'+ '、'.join(missing)+'状态；不能推断已通过。');unknown=True
    if problem:out.update(group='attention',label='内容或图片需要处理')
    elif unknown:pass
    elif all_active and eligible:out.update(group='active',label='英阿内容状态均有效')
    elif pending:out.update(group='pending',label='等待平台审核')
    else:out['notes'].append('各项状态尚未同时满足，需继续核对平台结果。')
    return out
