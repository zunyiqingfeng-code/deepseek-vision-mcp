window.__ModuleLoader__.load({
  id: '@dsh-local/vision-send-image',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })
    let React = require('react')

    var inject = ['slots']

    function apply(ctx) {
      var slots = ctx.slots
      if (slots === undefined) return

      function readDataUrl(file) {
        return new Promise(function (resolve, reject) {
          var reader = new FileReader()
          reader.onload = function () { resolve(String(reader.result)) }
          reader.onerror = function () { reject(reader.error || new Error('read failed')) }
          reader.readAsDataURL(file)
        })
      }

      async function upload(dataUrl) {
        var response = await fetch('/vision-upload', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ dataUrl: dataUrl }),
        })
        return await response.json()
      }

      slots.inject('conversation.input.left', function () {
        return slots.register(
          { name: 'conversation.input.left', id: 'vision-send-image', order: 10 },
          function (props) {
            var inputActions = props.inputActions
            var input = props.input
            var fileRef = React.useRef(null)
            var actionsRef = React.useRef(inputActions)
            actionsRef.current = inputActions
            var draftRef = React.useRef(input ? input.draft : '')
            draftRef.current = input ? input.draft : ''
            var state = React.useState({ busy: false, notice: '' })
            var busy = state[0].busy
            var notice = state[0].notice
            var setState = state[1]

            async function saveImageFile(file) {
              if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) {
                setState({ busy: false, notice: '仅支持 PNG/JPEG/WebP/GIF' })
                return
              }
              setState({ busy: true, notice: '上传中…' })
              try {
                var dataUrl = await readDataUrl(file)
                var result = await upload(dataUrl)
                if (result && result.ok) {
                  var draft = draftRef.current || ''
                  var pathLine = '图片：' + result.path
                  actionsRef.current.setDraft(draft ? draft + '\n' + pathLine : pathLine)
                  setState({ busy: false, notice: '✓ 已插入图片路径' })
                } else {
                  setState({ busy: false, notice: (result && result.error) || '保存失败' })
                }
              } catch (error) {
                setState({ busy: false, notice: '读取图片失败' })
              }
            }

            React.useEffect(function () {
              function onPaste(event) {
                var items = event.clipboardData && event.clipboardData.items
                if (!items) return
                var imageFile = null
                for (var i = 0; i < items.length; i++) {
                  var item = items[i]
                  if (item.kind === 'file' && item.type.indexOf('image/') === 0) {
                    imageFile = item.getAsFile()
                    break
                  }
                }
                if (!imageFile) return
                event.preventDefault()
                event.stopPropagation()
                saveImageFile(imageFile)
              }
              document.addEventListener('paste', onPaste, true)
              return function () { document.removeEventListener('paste', onPaste, true) }
            }, [])

            function onPick(event) {
              var file = event.target.files && event.target.files[0]
              if (!file) return
              if (fileRef.current) fileRef.current.value = ''
              saveImageFile(file)
            }

            return React.createElement('label', {
              style: {
                cursor: 'pointer',
                opacity: busy ? 0.5 : 1,
                display: 'inline-flex',
                alignItems: 'center',
                gap: '4px',
                userSelect: 'none',
                padding: '0 4px',
                fontSize: '13px',
              },
              title: notice || '发图：点此选图，或直接 Ctrl+V 粘贴图片',
            }, [
              React.createElement('input', {
                key: 'f',
                type: 'file',
                accept: 'image/png,image/jpeg,image/webp,image/gif',
                style: { display: 'none' },
                ref: fileRef,
                onChange: onPick,
              }),
              React.createElement('span', { key: 't' }, busy ? '⏳' : '📷'),
            ])
          },
        )
      })
    }

    exports.apply = apply
    exports.inject = inject
    return module.exports
  },
})
