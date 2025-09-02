def generate_bootstrap_html_table(df, title="ねぶかわウィークリー 成績一覧"):
    table_html = df.to_html(
        index=False,
        escape=False,
        table_id="results-table",
        classes="table table-hover align-middle"
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <title>{title}</title>
  <!-- Bootstrap 5 -->
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <!-- DataTables -->
  <link href="https://cdn.datatables.net/v/bs5/dt-2.0.7/datatables.min.css" rel="stylesheet"/>

  <style>
    body {{
      background-color: #f7fbff; /* 薄い水色背景 */
      color: #333;
      line-height: 1.6;
    }}
    .page-wrap {{
      max-width: 1100px;
      margin: 40px auto;
      padding: 0 16px;
    }}
    h1 {{
      margin-bottom: 20px;
      font-weight: 600;
      color: #0277bd; /* 深めの水色 */
    }}
    .card-like {{
      background: #ffffff;
      border: 1px solid #cce7f6;
      border-radius: 12px;
      box-shadow: 0 4px 10px rgba(0,0,0,.05);
      padding: 20px;
    }}

    /* テーブル見た目 */
    table.dataTable.table {{
      background: #ffffff;
      border-collapse: separate;
      border-spacing: 0;
    }}
    thead th {{
      text-align: center !important;
      background: #e1f5fe; /* 淡い水色 */
      color: #01579b;
    }}
    tbody td {{
      vertical-align: middle;
    }}
    td.num, th.num {{
      text-align: right !important;
      font-variant-numeric: tabular-nums;
    }}
    td.txt {{ text-align: left !important; }}

    /* ゼブラ */
    table.dataTable tbody tr:nth-child(odd) td {{
      background: #f9fcff;
    }}
    table.dataTable tbody tr:hover td {{
      background: #e1f5fe;
    }}

    /* DataTables コンポーネント */
    .dataTables_wrapper .dataTables_filter input,
    .dataTables_wrapper .dataTables_length select {{
      border: 1px solid #90caf9;
      border-radius: 6px;
      padding: 4px 8px;
    }}
    .dataTables_wrapper .dataTables_info {{
      color: #0277bd;
    }}
    .dataTables_wrapper .dataTables_paginate .paginate_button {{
      border-radius: 6px !important;
      border: 1px solid #90caf9 !important;
      background: #e1f5fe !important;
      color: #0277bd !important;
      margin: 0 2px;
      padding: 2px 8px !important;
    }}
    .dataTables_wrapper .dataTables_paginate .paginate_button.current {{
      background: #29b6f6 !important;
      border-color: #29b6f6 !important;
      color: #fff !important;
    }}
  </style>
</head>
<body>
  <div class="page-wrap">
    <h1>{title}</h1>
    <div class="card-like">
      {table_html}
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/jquery@3.7.1/dist/jquery.min.js"></script>
  <script src="https://cdn.datatables.net/v/bs5/dt-2.0.7/datatables.min.js"></script>

  <script>
    (function() {{
      const table = document.getElementById('results-table');
      if (!table) return;

      const numericHeaderNames = new Set(['回','Round','順位','Rank','BPI','Score','Score Rate (%)']);
      const headers = Array.from(table.querySelectorAll('thead th'));
      const numericIdx = [];
      headers.forEach((th, i) => {{
        const name = th.textContent.trim();
        if (numericHeaderNames.has(name)) {{
          th.classList.add('num');
          numericIdx.push(i);
        }}
      }});
      const rows = table.tBodies[0] ? Array.from(table.tBodies[0].rows) : [];
      rows.forEach(tr => {{
        Array.from(tr.cells).forEach((td, i) => {{
          if (numericIdx.includes(i)) td.classList.add('num');
          else td.classList.add('txt');
        }});
      }});

      let defaultOrder = [[0, 'asc']];
      const roundIdx = headers.findIndex(th => {{
        const t = th.textContent.trim();
        return t === '回' || t === 'Round';
      }});
      if (roundIdx >= 0) defaultOrder = [[roundIdx, 'asc']];

      new DataTable('#results-table', {{
        responsive: true,
        order: defaultOrder,
        pageLength: 25,
        language: {{
          search: "検索:",
          lengthMenu: "表示件数: _MENU_",
          info: "_TOTAL_ 件中 _START_ 〜 _END_ を表示",
          infoEmpty: "0 件中 0 〜 0 を表示",
          paginate: {{ first: "最初", last: "最後", next: "次へ", previous: "前へ" }},
          zeroRecords: "一致する記録が見つかりません"
        }},
        columnDefs: numericIdx.map(i => ({{
          targets: i,
          type: 'num'
        }}))
      }});
    }})();
  </script>
</body>
</html>"""
