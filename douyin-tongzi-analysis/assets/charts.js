// assets/charts.js
(function() {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var accent3 = style.getPropertyValue('--accent3').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();

  // --- Chart: Video Structure Timeline ---
  var chart1 = echarts.init(document.getElementById('chart-video-structure'), null, { renderer: 'svg' });
  
  var totalSeconds = 4 * 60 + 32; // 4分32秒
  
  chart1.setOption({
    animation: false,
    tooltip: {
      trigger: 'item',
      appendToBody: true,
      formatter: function(params) {
        var minutes = Math.floor(params.value / 60);
        var seconds = params.value % 60;
        return params.name + '<br/>时长：' + minutes + '分' + seconds + '秒<br/>占比：' + params.percent + '%';
      }
    },
    legend: {
      orient: 'vertical',
      right: '5%',
      top: 'center',
      textStyle: {
        color: muted,
        fontSize: 12
      },
      itemGap: 20
    },
    series: [
      {
        name: '视频结构',
        type: 'pie',
        radius: ['45%', '70%'],
        center: ['35%', '50%'],
        avoidLabelOverlap: false,
        itemStyle: {
          borderRadius: 6,
          borderColor: bg2,
          borderWidth: 3
        },
        label: {
          show: true,
          position: 'inside',
          formatter: '{d}%',
          color: '#fff',
          fontSize: 13,
          fontWeight: 600
        },
        labelLine: {
          show: false
        },
        data: [
          { 
            value: 32, 
            name: '引言（钩子开场）', 
            itemStyle: { color: accent }
          },
          { 
            value: 110, 
            name: '第一部分：研究用户需求', 
            itemStyle: { color: accent2 }
          },
          { 
            value: 78, 
            name: '第二部分：打磨视频开头', 
            itemStyle: { color: accent3 }
          },
          { 
            value: 52, 
            name: '第三部分：选题策略+引导', 
            itemStyle: { color: muted }
          }
        ]
      }
    ]
  });

  window.addEventListener('resize', function() {
    chart1.resize();
  });
})();
