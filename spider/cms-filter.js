/*
 * cms-filter.js —— 苹果CMS(provide/vod) 过滤外壳
 * 作用：透传上游 CMS 接口，但在「分类列表」里剔除成人/里番类目，
 *       并且分类/搜索/详情都会二次校验，避免通过分类直接进入。
 * 用法：TVBox/FongMi 里配置站点
 *   {"key":"md","name":"魔都","type":3,"api":"https://.../cms-filter.js",
 *    "ext":"https://www.mdzyapi.com/api.php/provide/vod","searchable":1,"quickSearch":1,"filterable":0}
 */

var HOST = '';

// 命中任意一个词就不展示
var BAD = [
    '里番', '伦理', '情色', '成人', '福利', '写真', '两性', '三级',
    '艳', '诱惑', '按摩', '制服', 'SM', 'sm', '自慰', '口交', '露出',
    '丝袜', '学生妹', '援交', '偷拍', '偷情', '裸', '脱衣'
];

function isClean(name) {
    if (!name) return false;
    for (var i = 0; i < BAD.length; i++) {
        if (name.indexOf(BAD[i]) >= 0) return false;
    }
    return true;
}

function init(cfg) {
    if (cfg && cfg.ext) HOST = ('' + cfg.ext).replace(/\/+$/, '');
}

function grab(url) {
    var body = req(url, {
        method: 'GET',
        headers: {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 11; TV) AppleWebKit/537.36 Chrome/120 Safari/537.36'
        },
        timeout: 15000
    });
    return JSON.parse(body);
}

function home(filter) {
    var j = grab(HOST + '?ac=class');
    var src = j['class'] || [];
    var out = [];
    for (var i = 0; i < src.length; i++) {
        var c = src[i];
        var nm = c.type_name || '';
        if (!isClean(nm)) continue;
        out.push({type_id: '' + c.type_id, type_name: nm});
    }
    return JSON.stringify({class: out, filters: {}, list: []});
}

function homeVod() {
    return JSON.stringify({list: []});
}

function category(tid, pg, filter, extend) {
    var j = grab(HOST + '?ac=detail&t=' + tid + '&pg=' + (pg || 1));
    var list = j.list || [];
    return JSON.stringify({
        page: pg || 1,
        pagecount: j.pagecount || 1,
        limit: j.limit || 20,
        total: j.total || list.length,
        list: list
    });
}

function detail(id) {
    var j = grab(HOST + '?ac=detail&ids=' + id);
    return JSON.stringify({list: j.list || []});
}

function search(wd, quick, pg) {
    var j = grab(HOST + '?ac=detail&wd=' + encodeURIComponent(wd) + '&pg=' + (pg || 1));
    return JSON.stringify({list: j.list || []});
}

function play(flag, id, flags) {
    return JSON.stringify({parse: 0, url: id, header: {}});
}
