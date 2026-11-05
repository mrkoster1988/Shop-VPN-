"""Customer-safe fulfillment progress. Never expose provider errors or credentials."""

def public(order):
    status=order['status'];kind=order.get('kind','purchase')
    if status=='active':
        title='Бонусные дни добавлены' if kind=='referral' else 'Пробный VPN готов' if kind=='trial' else 'Доступ выдан'
        return {'stage':'ready','title':title,'message':'Выдача заказа завершена. Текущий срок подписки проверьте в личном кабинете.','needs_support':False}
    if status=='paid':
        attempts=order.get('attempts',0)
        help_needed=attempts>=3
        stage='help' if help_needed else 'retrying' if attempts else 'queued'
        title=('Выдача задерживается · нужна помощь' if help_needed else 'Выдача задерживается · повторим попытку') if attempts else (
            'Добавляем бонусные дни' if kind=='referral' else 'Готовим пробный VPN' if kind=='trial' else 'Оплачено · готовим VPN')
        message='Заказ сохранён в очереди автоматической выдачи.'
        if kind=='purchase':message+=' Повторно оплачивать не нужно.'
        if help_needed:message+=' Напишите в поддержку и сообщите номер заказа.'
        return {'stage':stage,'title':title,'message':message,'needs_support':help_needed}
    if status=='canceled':
        return {'stage':'canceled','title':'Заказ отменён','message':'Если деньги были списаны, сообщите поддержке номер заказа.' if kind=='purchase' else 'Если нужна помощь, сообщите поддержке номер заказа.','needs_support':False}
    return {'stage':'waiting_payment','title':'Ожидает оплаты' if status=='pending' else 'Оплата не завершена',
            'message':'Доступ появится после подтверждения оплаты и выдачи подключения.','needs_support':False}
