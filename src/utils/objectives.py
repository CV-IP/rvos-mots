import torch
from utils.hungarian import softIoU, MaskedNLL, StableBalancedMaskedBCE, FocalLoss
import torch.nn as nn
from torch.autograd import Variable



class MaskedNLLLoss(nn.Module):
    def __init__(self, balance_weight=None):
        super(MaskedNLLLoss,self).__init__()
        self.balance_weight=balance_weight
    def forward(self, y_true, y_pred, sw):
        costs = MaskedNLL(y_true,y_pred, self.balance_weight).view(-1,1)
        costs = torch.masked_select(costs,sw.byte())
        return costs

class MaskedBCELoss(nn.Module):

    def __init__(self,balance_weight=None):
        super(MaskedBCELoss,self).__init__()
        self.balance_weight = balance_weight
    def forward(self, y_true, y_pred,sw):
        costs = StableBalancedMaskedBCE(y_true,y_pred,self.balance_weight).view(-1,1)
        costs = torch.masked_select(costs,sw.byte())
        return costs

class softIoULoss(nn.Module):

    def __init__(self):
        super(softIoULoss,self).__init__()
    def forward(self, y_true, y_pred, sw):
        costs = softIoU(y_true,y_pred).view(-1,1)
        if (sw.data > 0).any():
            costs = torch.mean(torch.masked_select(costs,sw.byte()))
        else:
            costs = torch.mean(costs)
        return costs
class focalLoss(nn.Module):

    def __init__(self):
        super(focalLoss,self).__init__()
    def forward(self, y_true, y_pred, sw):
        #costs = FocalLoss(y_true,y_pred).view(-1,1)
        y_pred = Variable(y_pred.cuda())
        y_true = Variable(y_true.cuda())
        costs = FocalLoss(gamma=2)(y_pred, y_true)
        if (sw.data > 0).any():
            costs = torch.mean(torch.masked_select(costs,sw.byte()))
        else:
            costs = torch.mean(costs)
        return costs

